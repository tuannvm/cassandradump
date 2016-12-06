import argparse
import sys
import itertools
import codecs
import shutil
import os


try:
    import cassandra
    import cassandra.concurrent
    from boto3.session import Session
except ImportError:
    sys.exit('Python Cassandra driver not installed. You might try \"pip install cassandra-driver\".')

from cassandra.auth import PlainTextAuthProvider #For protocol_version 2
from cassandra.cluster import Cluster

TIMEOUT = 120.0
FETCH_SIZE = 100
DOT_EVERY = 1000
CONCURRENT_BATCH_SIZE = 1000

args = None

def cql_type(v):
    try:
        return v.data_type.typename
    except AttributeError:
        return v.cql_type

def to_utf8(s):
    return codecs.decode(s, 'utf-8')

def log_quiet(msg):
    if not args.quiet:
        sys.stdout.write(msg)
        sys.stdout.flush()


def table_to_cqlfile(session, keyspace, tablename, flt, tableval, filep):
    if flt is None:
        query = 'SELECT * FROM "' + keyspace + '"."' + tablename + '"'
    else:
        query = 'SELECT * FROM ' + flt

    rows = session.execute(query)

    cnt = 0

    def make_non_null_value_encoder(typename):
        if typename == 'blob':
            return session.encoder.cql_encode_bytes
        elif typename.startswith('map'):
            return session.encoder.cql_encode_map_collection
        elif typename.startswith('set'):
            return session.encoder.cql_encode_set_collection
        elif typename.startswith('list'):
            return session.encoder.cql_encode_list_collection
        else:
            return session.encoder.cql_encode_all_types

    def make_value_encoder(typename):
        e = make_non_null_value_encoder(typename)
        return lambda v : session.encoder.cql_encode_all_types(v) if v is None else e(v)

    def make_value_encoders(tableval):
        return dict((to_utf8(k), make_value_encoder(cql_type(v))) for k, v in tableval.columns.iteritems())

    def make_row_encoder(tableevel):
        partitions = dict(
            (has_counter, list(to_utf8(k) for k, v in columns))
            for has_counter, columns in itertools.groupby(tableval.columns.iteritems(), lambda (k, v): cql_type(v) == 'counter')
        )

        keyspace_utf8 = to_utf8(keyspace)
        tablename_utf8 = to_utf8(tablename)

        counters = partitions.get(True, [])
        non_counters = partitions.get(False, [])
        columns = counters + non_counters

        if len(counters) > 0:
            def row_encoder(values):
                set_clause = ", ".join('%s = %s + %s' % (c, c,  values[c]) for c in counters if values[c] != 'NULL')
                where_clause = " AND ".join('%s = %s' % (c, values[c]) for c in non_counters)
                return 'UPDATE "%(keyspace)s"."%(tablename)s" SET %(set_clause)s WHERE %(where_clause)s' % dict(
                        keyspace = keyspace_utf8,
                        tablename = tablename_utf8,
                        where_clause = where_clause,
                        set_clause = set_clause,
                )
        else:
            columns = list(counters + non_counters)
            def row_encoder(values):
                return 'INSERT INTO "%(keyspace)s"."%(tablename)s" (%(columns)s) VALUES (%(values)s)' % dict(
                        keyspace = keyspace_utf8,
                        tablename = tablename_utf8,
                        columns = ', '.join('"{}"'.format(c) for c in columns if values[c]!="NULL"),
                        values = ', '.join(values[c] for c in columns if values[c]!="NULL"),
                )
        return row_encoder

    value_encoders = make_value_encoders(tableval)
    row_encoder = make_row_encoder(tableval)

    for row in rows:
        values = dict((to_utf8(k), to_utf8(value_encoders[k](v))) for k, v in row.iteritems())
        filep.write("%s;\n" % row_encoder(values))

        cnt += 1

        if (cnt % DOT_EVERY) == 0:
            log_quiet('.')

    if cnt > DOT_EVERY:
        log_quiet('\n')


def can_execute_concurrently(statement):
    if args.sync:
        return False

    if statement.upper().startswith('INSERT') or statement.upper().startswith('UPDATE'):
        return True
    else:
        return False


def import_data(session):
    f = codecs.open(args.import_file, 'r', encoding = 'utf-8')

    cnt = 0

    statement = ''
    concurrent_statements = []

    for line in f:
        statement += line
        if statement.endswith(";\n"):
            if can_execute_concurrently(statement):
                concurrent_statements.append((statement, None))

                if len(concurrent_statements) >= CONCURRENT_BATCH_SIZE:
                    cassandra.concurrent.execute_concurrent(session, concurrent_statements)
                    concurrent_statements = []
            else:
                if len(concurrent_statements) > 0:
                    cassandra.concurrent.execute_concurrent(session, concurrent_statements)
                    concurrent_statements = []

                session.execute(statement)

            statement = ''

            cnt += 1
            if (cnt % DOT_EVERY) == 0:
                log_quiet('.')

    if len(concurrent_statements) > 0:
        cassandra.concurrent.execute_concurrent(session, concurrent_statements)

    if statement != '':
        session.execute(statement)

    if cnt > DOT_EVERY:
        log_quiet('\n')

    f.close()


def get_keyspace_or_fail(session, keyname):
    keyspace = session.cluster.metadata.keyspaces.get(keyname)

    if not keyspace:
        sys.stderr.write('Can\'t find keyspace "' + keyname + '"\n')
        sys.exit(1)

    return keyspace


def get_column_family_or_fail(keyspace, tablename):
    tableval = keyspace.tables.get(tablename)

    if not tableval:
        sys.stderr.write('Can\'t find table "' + tablename + '"\n')
        sys.exit(1)

    return tableval


def export_data(session):
    selection_options = 0

    if args.keyspace is not None:
        selection_options += 1

    if args.cf is not None:
        selection_options += 1

    if args.filter is not None:
        selection_options += 1

    if selection_options > 1:
        sys.stderr.write('--cf, --keyspace and --filter can\'t be combined\n')
        sys.exit(1)

    f = codecs.open(args.export_file, 'w', encoding = 'utf-8')

    keyspaces = None

    if selection_options == 0:
        log_quiet('Exporting all keyspaces\n')
        keyspaces = []
        for keyspace in session.cluster.metadata.keyspaces.keys():
            if keyspace not in ('system', 'system_traces'):
                keyspaces.append(keyspace)

    if args.keyspace is not None:
        keyspaces = args.keyspace

    if keyspaces is not None:
        for keyname in keyspaces:
            keyspace = get_keyspace_or_fail(session, keyname)

            if not args.no_create:
                log_quiet('Exporting schema for keyspace ' + keyname + '\n')
                f.write('DROP KEYSPACE IF EXISTS "' + keyname + '";\n')
                f.write(keyspace.export_as_string() + '\n')

            for tablename, tableval in keyspace.tables.iteritems():
                if tableval.is_cql_compatible:
                    if not args.no_insert:
                        log_quiet('Exporting data for column family ' + keyname + '.' + tablename + '\n')
                        table_to_cqlfile(session, keyname, tablename, None, tableval, f)

    if args.cf is not None:
        for cf in args.cf:
            if '.' not in cf:
                sys.stderr.write('Invalid keyspace.column_family input\n')
                sys.exit(1)

            keyname = cf.split('.')[0]
            tablename = cf.split('.')[1]

            keyspace = get_keyspace_or_fail(session, keyname)
            tableval = get_column_family_or_fail(keyspace, tablename)

            if tableval.is_cql_compatible:
                if not args.no_create:
                    log_quiet('Exporting schema for column family ' + keyname + '.' + tablename + '\n')
                    f.write('DROP TABLE IF EXISTS "' + keyname + '"."' + tablename + '";\n')
                    f.write(tableval.export_as_string() + ';\n')

                if not args.no_insert:
                    log_quiet('Exporting data for column family ' + keyname + '.' + tablename + '\n')
                    table_to_cqlfile(session, keyname, tablename, None, tableval, f)

    if args.filter is not None:
        for flt in args.filter:
            stripped = flt.strip()
            cf = stripped.split(' ')[0]

            if '.' not in cf:
                sys.stderr.write('Invalid input\n')
                sys.exit(1)

            keyname = cf.split('.')[0]
            tablename = cf.split('.')[1]


            keyspace = get_keyspace_or_fail(session, keyname)
            tableval = get_column_family_or_fail(keyspace, tablename)

            if not tableval:
                sys.stderr.write('Can\'t find table "' + tablename + '"\n')
                sys.exit(1)

            if not args.no_insert:
                log_quiet('Exporting data for filter "' + stripped + '"\n')
                table_to_cqlfile(session, keyname, tablename, stripped, tableval, f)

    f.close()

    if args.compress:
        shutil.make_archive(args.export_file, 'zip', None, args.export_file)

    if args.s3_upload:
        bucket = s3_upload(args.s3_bucket_name, args.aws_access_key, args.aws_secret_key)
        if args.compress:
            bucket.upload_file(args.export_file + '.zip', args.export_file + '.zip')
        else:
            bucket.upload_file(args.export_file, args.export_file)             



def s3_upload(bucket_name, access_key, secret_key):
    conn = Session(
        aws_access_key_id=access_key,aws_secret_access_key=secret_key,
        )

    s3 = conn.resource('s3')
    bucket = s3.Bucket(bucket_name)
    return bucket

def get_credentials(self):
    return {'username': args.username, 'password': args.password}

def setup_cluster():
    if args.host is None:
        nodes = ['localhost']
    else:
        nodes = [args.host]

    if args.port is None:
        port = 9042
    else:
        port = args.port

    cluster = None

    if args.protocol_version is not None:
        auth = None

        if args.username is not None and args.password is not None:
            if args.protocol_version == 1:
                auth = get_credentials
            elif args.protocol_version > 1:
                auth = PlainTextAuthProvider(username=args.username, password=args.password)

        cluster = Cluster(contact_points=nodes, port=port, protocol_version=args.protocol_version, auth_provider=auth, load_balancing_policy=cassandra.policies.WhiteListRoundRobinPolicy(nodes))
    else:
        cluster = Cluster(contact_points=nodes, port=port, load_balancing_policy=cassandra.policies.WhiteListRoundRobinPolicy(nodes))

    session = cluster.connect()

    session.default_timeout = TIMEOUT
    session.default_fetch_size = FETCH_SIZE
    session.row_factory = cassandra.query.ordered_dict_factory
    return session


def cleanup_cluster(session):
    session.cluster.shutdown()
    session.shutdown()

def cleanup_export_file(file_name):
    for file in file_name:
        os.remove(file)

def main():
    global args

    parser = argparse.ArgumentParser(description='A data exporting tool for Cassandra inspired from mysqldump, with some added slice and dice capabilities.')
    parser.add_argument('--cf', help='export a column family. The name must include the keyspace, e.g. "system.schema_columns". Can be specified multiple times', action='append')
    parser.add_argument('--export-file', help='export data to the specified file')
    parser.add_argument('--filter', help='export a slice of a column family according to a CQL filter. This takes essentially a typical SELECT query stripped of the initial "SELECT ... FROM" part (e.g. "system.schema_columns where keyspace_name =\'OpsCenter\'", and exports only that data. Can be specified multiple times', action='append')
    parser.add_argument('--host', help='the address of a Cassandra node in the cluster (localhost if omitted)')
    parser.add_argument('--port', help='the port of a Cassandra node in the cluster (9042 if omitted)')
    parser.add_argument('--import-file', help='import data from the specified file')
    parser.add_argument('--keyspace', help='export a keyspace along with all its column families. Can be specified multiple times', action='append')
    parser.add_argument('--no-create', help='don\'t generate create (and drop) statements', action='store_true')
    parser.add_argument('--no-insert', help='don\'t generate insert statements', action='store_true')
    parser.add_argument('--password', help='set password for authentication (only if protocol-version is set)')
    parser.add_argument('--protocol-version', help='set protocol version (required for authentication)', type=int)
    parser.add_argument('--quiet', help='quiet progress logging', action='store_true')
    parser.add_argument('--sync', help='import data in synchronous mode (default asynchronous)', action='store_true')
    parser.add_argument('--username', help='set username for auth (only if protocol-version is set)')
    parser.add_argument('--s3-upload', help='upload export file to amazon s3', action='store_true')
    parser.add_argument('--s3-bucket-name', help='define s3 bucket name')
    parser.add_argument('--aws-access-key', help='define aws access key')
    parser.add_argument('--aws-secret-key', help='define aws secret key')
    parser.add_argument('--compress', help='enable compression to export file', action='store_true')
    parser.add_argument('--clean-up', help='remove export file after upload', action='store_true')
    args = parser.parse_args()

    if args.import_file is None and args.export_file is None:
        sys.stderr.write('--import-file or --export-file must be specified\n')
        sys.exit(1)

    if args.import_file is not None and args.export_file is not None:
        sys.stderr.write('--import-file and --export-file can\'t be specified at the same time\n')
        sys.exit(1)

    if args.s3_upload and (args.aws_access_key is None or args.aws_secret_key is None or args.s3_bucket_name is None):
        sys.stderr.write('--aws-access-key, --aws-secret-key or --s3-bucket-name must be specified when --s3-upload is enable\n')
        sys.exit(1)

    session = setup_cluster()

    if args.import_file:
        import_data(session)
    elif args.export_file:
        export_data(session)

    cleanup_cluster(session)

    if args.clean_up:
        if args.compress:
            cleanup_export_file([args.export_file +".zip", args.export_file])
        else:
            cleanup_export_file([args.export_file])
        


if __name__ == '__main__':
    main()
