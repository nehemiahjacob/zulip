from __future__ import absolute_import
from __future__ import print_function
import datetime
from boto.s3.key import Key
from boto.s3.connection import S3Connection
from django.conf import settings
from django.forms.models import model_to_dict
from django.utils import timezone
from collections import defaultdict
import logging
import os
import ujson
import shutil
import subprocess
import tempfile
from zerver.lib.avatar import user_avatar_hash
from zerver.lib.create_user import random_api_key
from zerver.models import UserProfile, Realm, Client, Huddle, Stream, \
    UserMessage, Subscription, Message, RealmEmoji, RealmFilter, \
    RealmAlias, Recipient, DefaultStream, get_user_profile_by_id, \
    UserPresence, UserActivity, UserActivityInterval, get_user_profile_by_email, \
    get_display_recipient, Attachment
from zerver.lib.parallel import run_parallel
from zerver.lib.utils import mkdir_p
from six.moves import range
from typing import Any, Dict

realm_tables = [("zerver_defaultstream", DefaultStream),
                ("zerver_realmemoji", RealmEmoji),
                ("zerver_realmalias", RealmAlias),
                ("zerver_realmfilter", RealmFilter)]

def floatify_datetime_fields(data, table, field):
    for item in data[table]:
        orig_dt = item[field]
        if orig_dt is None:
            continue
        if timezone.is_naive(orig_dt):
            logging.warning("Naive datetime:", item)
            dt = timezone.make_aware(orig_dt)
        else:
            dt = orig_dt
        utc_naive  = dt.replace(tzinfo=None) - dt.utcoffset()
        item[field] = (utc_naive - datetime.datetime(1970, 1, 1)).total_seconds()

# Export common, public information about the realm that we can share
# with all realm users
def export_realm_data(realm, response):
    response['zerver_realm'] = [model_to_dict(x) for x in Realm.objects.filter(id=realm.id)]
    floatify_datetime_fields(response, 'zerver_realm', 'date_created')

    for (table, model) in realm_tables:
        response[table] = [model_to_dict(x) for x in
                           model.objects.filter(realm_id=realm.id)] # type: ignore # Iterating through types
    response["zerver_client"] = [model_to_dict(x) for x in Client.objects.select_related()]

# To export only some users, you can tweak the below UserProfile query
# to give the target users, but then you should create any users not
# being exported in a separate
# response['zerver_userprofile_mirrordummy'] export so that
# conversations with those users can still be exported.
def export_with_admin_auth(realm, response, include_invite_only=True, include_private=True):
    response['zerver_userprofile'] = [model_to_dict(x, exclude=["password", "api_key"])
                                      for x in UserProfile.objects.filter(realm=realm)]
    if realm.domain == "zulip.com":
        response['zerver_userprofile_crossrealm'] = []
    else:
        response['zerver_userprofile_crossrealm'] = [dict(email=x.email, id=x.id) for x in [
        get_user_profile_by_email(settings.NOTIFICATION_BOT),
        get_user_profile_by_email(settings.EMAIL_GATEWAY_BOT),
        get_user_profile_by_email(settings.WELCOME_BOT),
    ]]
    floatify_datetime_fields(response, 'zerver_userprofile', 'date_joined')
    floatify_datetime_fields(response, 'zerver_userprofile', 'last_login')
    floatify_datetime_fields(response, 'zerver_userprofile', 'last_reminder')
    user_profile_ids = set(userprofile["id"] for userprofile in response['zerver_userprofile'])
    user_recipients = [model_to_dict(x)
                       for x in Recipient.objects.filter(type=Recipient.PERSONAL)
                       if x.type_id in user_profile_ids]
    user_recipient_ids = set(x["id"] for x in user_recipients)
    user_subscription_query = Subscription.objects.filter(user_profile__in=user_profile_ids,
                                                          recipient_id__in=user_recipient_ids)
    user_subscription_dicts = [model_to_dict(x) for x in user_subscription_query]

    response["zerver_userpresence"] = [model_to_dict(x) for x in
                                       UserPresence.objects.filter(user_profile__in=user_profile_ids)]
    floatify_datetime_fields(response, 'zerver_userpresence', 'timestamp')

    response["zerver_useractivity"] = [model_to_dict(x) for x in
                                       UserActivity.objects.filter(user_profile__in=user_profile_ids)]
    floatify_datetime_fields(response, 'zerver_useractivity', 'last_visit')

    response["zerver_useractivityinterval"] = [model_to_dict(x) for x in
                                               UserActivityInterval.objects.filter(user_profile__in=user_profile_ids)]
    floatify_datetime_fields(response, 'zerver_useractivityinterval', 'start')
    floatify_datetime_fields(response, 'zerver_useractivityinterval', 'end')

    stream_query = Stream.objects.filter(realm=realm)
    if not include_invite_only:
        stream_query = stream_query.filter(invite_only=False)
    response['zerver_stream'] = [model_to_dict(x, exclude=["email_token"]) for x in stream_query]
    floatify_datetime_fields(response, 'zerver_stream', 'date_created')
    stream_ids = set(x["id"] for x in response['zerver_stream'])
    stream_recipients = [model_to_dict(x)
                         for x in Recipient.objects.filter(type=Recipient.STREAM,
                                                           type_id__in=stream_ids)]

    stream_recipient_ids = set(x["id"] for x in stream_recipients)

    stream_subscription_query = Subscription.objects.filter(user_profile__in=user_profile_ids,
                                                            recipient_id__in=stream_recipient_ids)
    stream_subscription_dicts = [model_to_dict(x) for x in stream_subscription_query]

    if include_private:
        # First we get all huddles involving someone in the realm.
        realm_huddle_subs = Subscription.objects.select_related("recipient").filter(recipient__type=Recipient.HUDDLE,
                                                                                    user_profile__in=user_profile_ids)
        realm_huddle_recipient_ids = set(sub.recipient_id for sub in realm_huddle_subs)

        # Mark all Huddles whose recipient ID contains a cross-realm user.
        unsafe_huddle_recipient_ids = set()
        for sub in Subscription.objects.select_related().filter(recipient__in=realm_huddle_recipient_ids):
            if sub.user_profile.realm != realm:
                # In almost every case the other realm will be zulip.com
                unsafe_huddle_recipient_ids.add(sub.recipient_id)

        # Now filter down to just those huddles that are entirely within the realm.
        #
        # This is important for ensuring that the User objects needed
        # to import it on the other end exist (since we're only
        # exporting the users from this realm), at the cost of losing
        # some of these cross-realm messages.
        huddle_subs = [sub for sub in realm_huddle_subs if sub.recipient_id not in unsafe_huddle_recipient_ids]
        huddle_recipient_ids = set(sub.recipient_id for sub in huddle_subs)
        huddle_ids = set(sub.recipient.type_id for sub in huddle_subs)

        huddle_subscription_dicts = [model_to_dict(x) for x in huddle_subs]
        huddle_recipients = [model_to_dict(r) for r in Recipient.objects.filter(id__in=huddle_recipient_ids)]
        response['zerver_huddle'] = [model_to_dict(h) for h in Huddle.objects.filter(id__in=huddle_ids)]
    else:
        huddle_recipients = []
        huddle_subscription_dicts = []

    response["zerver_recipient"] = user_recipients + stream_recipients + huddle_recipients
    response["zerver_subscription"] = user_subscription_dicts + stream_subscription_dicts + huddle_subscription_dicts

    response["zerver_attachment"] = [model_to_dict(x) for x in
                                     Attachment.objects.filter(realm=realm)]
    floatify_datetime_fields(response, 'zerver_attachment', 'create_time')

def fetch_usermessages(realm, message_ids, user_profile_ids, message_filename):
    # UserMessage export security rule: You can export UserMessages
    # for the messages you exported for the users in your realm.
    user_message_query = UserMessage.objects.filter(user_profile__realm=realm,
                                                    message_id__in=message_ids)
    user_message_chunk = []
    for user_message in user_message_query:
        if user_message.user_profile_id not in user_profile_ids:
            continue
        user_message_obj = model_to_dict(user_message)
        user_message_obj['flags_mask'] = user_message.flags.mask
        del user_message_obj['flags']
        user_message_chunk.append(user_message_obj)
    logging.info("Fetched UserMessages for %s" % (message_filename,))
    return user_message_chunk

def export_usermessages_batch(input_path, output_path):
    """As part of the system for doing parallel exports, this runs on one
    batch of Message objects and adds the corresponding UserMessage
    objects."""
    with open(input_path, "r") as input_file:
        output = ujson.loads(input_file.read())
    message_ids = [item['id'] for item in output['zerver_message']]
    user_profile_ids = set(output['zerver_userprofile_ids'])
    del output['zerver_userprofile_ids']
    realm = Realm.objects.get(id=output['realm_id'])
    del output['realm_id']
    output['zerver_usermessage'] = fetch_usermessages(realm, message_ids, user_profile_ids, output_path)
    write_message_export(output_path, output)
    os.unlink(input_path)

def write_message_export(message_filename, output):
    with open(message_filename, "w") as f:
        f.write(ujson.dumps(output, indent=4))
    logging.info("Dumped to %s" % (message_filename,))

def export_messages(realm, user_profile_ids, recipient_ids,
                    chunk_size=1000, output_dir=None,
                    threads=0):
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="zulip-export")

    # Basic security rule: You can export everything sent by someone
    # in your realm export (members of your realm plus Zulip realm
    # bots) to a recipient object you're exporting (that is thus also
    # in your realm).
    #
    # TODO: In theory, you should be able to export messages in
    # cross-realm PM threads; currently, this only exports cross-realm
    # messages received by your realm that were sent by Zulip system
    # bots (e.g. emailgateway, notification-bot).
    message_query = Message.objects.filter(sender__in=user_profile_ids,
                                           recipient__in=recipient_ids).order_by("id")

    min_id = -1
    dump_file_id = 1
    while True:
        actual_query = message_query.filter(id__gt=min_id)[0:chunk_size]
        message_chunk = [model_to_dict(m) for m in actual_query]
        message_ids = set(m['id'] for m in message_chunk)

        if len(message_chunk) == 0:
            break

        message_filename = os.path.join(output_dir, "messages-%06d.json" % (dump_file_id,))
        logging.info("Fetched Messages for %s" % (message_filename,))

        output = {'zerver_message': message_chunk}
        floatify_datetime_fields(output, 'zerver_message', 'pub_date')
        floatify_datetime_fields(output, 'zerver_message', 'last_edit_time')

        if threads > 0:
            message_filename += '.partial'
            output['zerver_userprofile_ids'] = list(user_profile_ids)
            output['realm_id'] = realm.id
        else:
            user_message_chunk = fetch_usermessages(realm, message_ids, user_profile_ids,
                                                    message_filename)
            output['zerver_usermessage'] = user_message_chunk

        write_message_export(message_filename, output)
        min_id = max(message_ids)
        dump_file_id += 1

    # TODO: Add asserts that every message was sent in the realm and every recipient is available above.

def export_bucket(realm, bucket_name, output_dir, avatar_bucket=False):
    conn = S3Connection(settings.S3_KEY, settings.S3_SECRET_KEY)
    bucket = conn.get_bucket(bucket_name, validate=True)
    records = []

    logging.info("Downloading uploaded files from %s" % (bucket_name))

    avatar_hash_values = set()
    user_ids = set()
    if avatar_bucket:
        bucket_list = bucket.list()
        for user_profile in UserProfile.objects.filter(realm=realm):
            avatar_hash = user_avatar_hash(user_profile.email)
            avatar_hash_values.add(avatar_hash)
            avatar_hash_values.add(avatar_hash + ".original")
            user_ids.add(user_profile.id)
    else:
        bucket_list = bucket.list(prefix="%s/" % (realm.id,))

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    if settings.EMAIL_GATEWAY_BOT is not None:
        email_gateway_bot = get_user_profile_by_email(settings.EMAIL_GATEWAY_BOT)
    else:
        email_gateway_bot = None

    count = 0
    for bkey in bucket_list:
        if avatar_bucket and bkey.name not in avatar_hash_values:
            continue
        key = bucket.get_key(bkey.name)

        # This can happen if an email address has moved realms
        if 'realm_id' in key.metadata and key.metadata['realm_id'] != str(realm.id):
            if email_gateway_bot is None or key.metadata['user_profile_id'] != str(email_gateway_bot.id):
                raise Exception("Key metadata problem: %s %s / %s" % (key.name, key.metadata, realm.id))
            # Email gateway bot sends messages, potentially including attachments, cross-realm.
            print("File uploaded by email gateway bot: %s / %s" % (key.name, key.metadata))
        elif avatar_bucket:
            if 'user_profile_id' not in key.metadata:
                raise Exception("Missing user_profile_id in key metadata: %s" % (key.metadata,))
            if int(key.metadata['user_profile_id']) not in user_ids:
                raise Exception("Wrong user_profile_id in key metadata: %s" % (key.metadata,))
        elif 'realm_id' not in key.metadata:
            raise Exception("Missing realm_id in key metadata: %s" % (key.metadata,))

        record = dict(s3_path=key.name, bucket=bucket_name,
                      size=key.size, last_modified=key.last_modified,
                      content_type=key.content_type, md5=key.md5)
        record.update(key.metadata)

        # A few early avatars don't have 'realm_id' on the object; fix their metadata
        user_profile = get_user_profile_by_id(record['user_profile_id'])
        if 'realm_id' not in record:
            record['realm_id'] = user_profile.realm_id
        record['user_profile_email'] = user_profile.email

        if avatar_bucket:
            dirname = output_dir
            filename = os.path.join(dirname, key.name)
            record['path'] = key.name
        else:
            fields = key.name.split('/')
            if len(fields) != 3:
                raise Exception("Suspicious key %s" % (key.name))
            dirname = os.path.join(output_dir, fields[1])
            filename = os.path.join(dirname, fields[2])
            record['path'] = os.path.join(fields[1], fields[2])

        if not os.path.exists(dirname):
            os.makedirs(dirname)
        key.get_contents_to_filename(filename)

        records.append(record)
        count += 1

        if (count % 100 == 0):
            logging.info("Finished %s" % (count,))

    with open(os.path.join(output_dir, "records.json"), "w") as records_file:
        ujson.dump(records, records_file, indent=4)

def export_uploads_local(realm, output_dir):
    export_uploads_local_helper(realm, os.path.join(output_dir, "uploads"),
                                os.path.join(settings.LOCAL_UPLOADS_DIR, "files"))
    # TODO: Export local avatars as well.

def export_uploads_local_helper(realm, output_dir, local_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    count = 0
    records = []
    for attachment in Attachment.objects.all():
        local_path = os.path.join(local_dir, attachment.path_id)
        output_path = os.path.join(output_dir, attachment.path_id)
        mkdir_p(os.path.dirname(output_path))
        subprocess.check_call(["cp", "-a", local_path, output_path])
        stat = os.stat(local_path)
        record = dict(realm_id=attachment.realm.id,
                      user_profile_id=attachment.owner.id,
                      user_profile_email=attachment.owner.email,
                      s3_path=attachment.path_id,
                      path=attachment.path_id,
                      size=stat.st_size,
                      last_modified=stat.st_mtime,
                      content_type=None)
        records.append(record)

        count += 1

        if (count % 100 == 0):
            logging.info("Finished %s" % (count,))
    with open(os.path.join(output_dir, "records.json"), "w") as records_file:
        ujson.dump(records, records_file, indent=4)

def export_uploads(realm, output_dir):
    os.makedirs(os.path.join(output_dir, "uploads"))
    export_bucket(realm, settings.S3_AVATAR_BUCKET, os.path.join(output_dir, "avatars"), True)
    export_bucket(realm, settings.S3_AUTH_UPLOADS_BUCKET, os.path.join(output_dir, "uploads"))

def do_export_realm(realm, output_dir, threads=0):
    response = {} # type: Dict[str, Any]

    logging.info("Exporting realm configuration")
    export_realm_data(realm, response)
    logging.info("Exporting core realm data")
    export_with_admin_auth(realm, response)
    export_file = os.path.join(output_dir, "realm.json")
    with open(export_file, "w") as f:
        f.write(ujson.dumps(response, indent=4))

    logging.info("Exporting uploaded files and avatars")
    if not settings.LOCAL_UPLOADS_DIR:
        export_uploads(realm, output_dir)
    else:
        export_uploads_local(realm, output_dir)

    user_profile_ids = set(x["id"] for x in response['zerver_userprofile'] +
                           response['zerver_userprofile_crossrealm'])
    recipient_ids = set(x["id"] for x in response['zerver_recipient'])
    logging.info("Exporting messages")
    export_messages(realm, user_profile_ids, recipient_ids, output_dir=output_dir,
                    threads=threads)
    if threads > 0:
        # Start parallel jobs to export the UserMessage objects
        def run_job(shard):
            subprocess.call(["./manage.py", 'export_usermessage_batch', '--path',
                             output_dir, '--thread', shard])
            return 0
        for (status, job) in run_parallel(run_job, [str(x) for x in range(0, threads)], threads=threads):
            print("Shard %s finished, status %s" % (job, status))
            pass
    logging.info("Finished exporting %s" % (realm.domain))

def do_export_user(user_profile, output_dir):
    response = {} # type: Dict[str, Any]

    export_single_user(user_profile, response)
    export_file = os.path.join(output_dir, "user.json")
    with open(export_file, "w") as f:
        f.write(ujson.dumps(response, indent=4))
    logging.info("Exporting messages")
    export_messages_single_user(user_profile, output_dir=output_dir)

def export_single_user(user_profile, response):
    response['zerver_userprofile'] = [model_to_dict(x, exclude=["password", "api_key"])
                                      for x in [user_profile]]
    floatify_datetime_fields(response, 'zerver_userprofile', 'date_joined')
    floatify_datetime_fields(response, 'zerver_userprofile', 'last_login')
    floatify_datetime_fields(response, 'zerver_userprofile', 'last_reminder')

    response["zerver_subscription"] = [model_to_dict(x) for x in
                                       Subscription.objects.filter(user_profile=user_profile)]
    recipient_ids = set(s["recipient"] for s in response["zerver_subscription"])
    response["zerver_recipient"] = [model_to_dict(x) for x in
                                    Recipient.objects.filter(id__in=recipient_ids)]
    stream_ids = set(x["type_id"] for x in response["zerver_recipient"] if x["type"] == Recipient.STREAM)

    stream_query = Stream.objects.filter(id__in=stream_ids)
    response['zerver_stream'] = [model_to_dict(x, exclude=["email_token"]) for x in stream_query]
    floatify_datetime_fields(response, 'zerver_stream', 'date_created')

def export_messages_single_user(user_profile, chunk_size=1000, output_dir=None):
    user_message_query = UserMessage.objects.filter(user_profile=user_profile)
    min_id = -1
    dump_file_id = 1
    while True:
        actual_query = user_message_query.select_related("message", "message__sending_client").filter(id__gt=min_id)[0:chunk_size]
        user_message_chunk = [um for um in actual_query]
        user_message_ids = set(um.id for um in user_message_chunk)

        if len(user_message_chunk) == 0:
            break

        message_chunk = []
        for user_message in user_message_chunk:
            item = model_to_dict(user_message.message)
            item['flags'] = user_message.flags_list()
            item['flags_mask'] = user_message.flags.mask
            # Add a few nice, human-readable details
            item['sending_client_name'] = user_message.message.sending_client.name
            item['display_recipient'] = get_display_recipient(user_message.message.recipient)
            message_chunk.append(item)

        message_filename = os.path.join(output_dir, "messages-%06d.json" % (dump_file_id,))
        logging.info("Fetched Messages for %s" % (message_filename,))

        output = {'zerver_message': message_chunk}
        floatify_datetime_fields(output, 'zerver_message', 'pub_date')
        floatify_datetime_fields(output, 'zerver_message', 'last_edit_time')

        write_message_export(message_filename, output)
        min_id = max(user_message_ids)
        dump_file_id += 1

# Code from here is the realm import code path

id_maps = defaultdict(dict) # type: defaultdict[str, Dict[int, int]]

def fix_datetime_fields(data, table, field_name):
    for item in data[table]:
        if item[field_name] is None:
            item[field_name] = None
        else:
            v = datetime.datetime.utcfromtimestamp(item[field_name])
            item[field_name] = timezone.make_aware(v, timezone=timezone.utc)

def fix_foreign_keys(data, table, field_name, id_map_table = ''):
    for item in data[table]:
        val = item[field_name]
        new_val = id_maps[id_map_table].get(item[field_name], val)
        item[field_name + "_id"] = new_val
        del item[field_name]

def fix_bitfield_keys(data, table, field_name):
    for item in data[table]:
        item[field_name] = item[field_name + '_mask']
        del item[field_name + '_mask']

def bulk_import_model(data, model, table, dump_file_id=None):
    model.objects.bulk_create(model(**item) for item in data[table])
    if dump_file_id is None:
        logging.info("Successfully imported %s from %s." % (model, table))
    else:
        logging.info("Successfully imported %s from %s[%s]." % (model, table, dump_file_id))

# Client is a table shared by multiple realms, so in order to
# correctly import multiple realms into the same server, we need to
# check if a Client object already exists, and so we need to support
# remap all Client IDs to the values in the new DB.
def bulk_import_client(data, model, table):
    for item in data[table]:
        try:
            client = Client.objects.get(name=item['name'])
        except Client.DoesNotExist:
            client = Client.objects.create(name=item['name'])
        id_maps['client'][item['id']] = client.id

def import_uploads_local(import_dir, avatar_bucket=False):
    records_filename = os.path.join(import_dir, "records.json")
    with open(records_filename) as records_file:
        records = ujson.loads(records_file.read())

    for record in records:
        if avatar_bucket:
            # For avatars, we need to rehash the user's email with the
            # new server's avatar salt
            avatar_hash = user_avatar_hash(record['user_profile_email'])
            file_path = os.path.join(settings.LOCAL_UPLOADS_DIR, "avatars", avatar_hash)
            if record['s3_path'].endswith('.original'):
                file_path += '.original'
            else:
                file_path += '.png'
        else:
            file_path = os.path.join(settings.LOCAL_UPLOADS_DIR, "files", record['s3_path'])

        orig_file_path = os.path.join(import_dir, record['path'])
        if not os.path.exists(os.path.dirname(file_path)):
            subprocess.check_call(["mkdir", "-p", os.path.dirname(file_path)])
        shutil.copy(orig_file_path, file_path)

def import_uploads_s3(bucket_name, import_dir, avatar_bucket=False):
    conn = S3Connection(settings.S3_KEY, settings.S3_SECRET_KEY)
    bucket = conn.get_bucket(bucket_name, validate=True)

    records_filename = os.path.join(import_dir, "records.json")
    with open(records_filename) as records_file:
        records = ujson.loads(records_file.read())

    for record in records:
        key = Key(bucket)

        if avatar_bucket:
            # For avatars, we need to rehash the user's email with the
            # new server's avatar salt
            avatar_hash = user_avatar_hash(record['user_profile_email'])
            key.key = avatar_hash
            if record['s3_path'].endswith('.original'):
                key.key += '.original'
        else:
            key.key = record['s3_path']

        user_profile_id = int(record['user_profile_id'])
        # Support email gateway bot and other cross-realm messages
        if user_profile_id in id_maps["user_profile"]:
            logging.info("Uploaded by ID mapped user: %s!" % (user_profile_id,))
            user_profile_id = id_maps["user_profile"][user_profile_id]
        user_profile = get_user_profile_by_id(user_profile_id)
        key.set_metadata("user_profile_id", str(user_profile.id))
        key.set_metadata("realm_id", str(user_profile.realm.id))
        key.set_metadata("orig_last_modified", record['last_modified'])

        headers = {'Content-Type': key['content_type']}

        key.set_contents_from_filename(os.path.join(import_dir, record['path']), headers=headers)

def import_uploads(import_dir, avatar_bucket=False):
    if avatar_bucket:
        logging.info("Importing avatars")
    else:
        logging.info("Importing uploaded files")
    if settings.LOCAL_UPLOADS_DIR:
        return import_uploads_local(import_dir, avatar_bucket=avatar_bucket)
    else:
        if avatar_bucket:
            bucket_name = settings.S3_AVATAR_BUCKET
        else:
            bucket_name = settings.S3_AUTH_UPLOADS_BUCKET
        return import_uploads_s3(bucket_name, import_dir, avatar_bucket=avatar_bucket)

# Importing data suffers from a difficult ordering problem because of
# models that reference each other circularly.  Here is a correct order.
#
# * Client [no deps]
# * Realm [-notifications_stream]
# * Stream [only depends on realm]
# * Realm's notifications_stream
# * Now can do all realm_tables
# * UserProfile, in order by ID to avoid bot loop issues
# * Huddle
# * Recipient
# * Subscription
# * Message
# * UserMessage
#
# Because the Python object => JSON conversion process is not fully
# faithful, we have to use a set of fixers (e.g. on DateTime objects
# and Foreign Keys) to do the import correctly.
def do_import_realm(import_dir):
    logging.info("Importing realm dump %s" % (import_dir,))
    if not os.path.exists(import_dir):
        raise Exception("Missing import directory!")

    realm_data_filename = os.path.join(import_dir, "realm.json")
    if not os.path.exists(realm_data_filename):
        raise Exception("Missing realm.json file!")

    logging.info("Importing realm data from %s" % (realm_data_filename,))
    with open(realm_data_filename) as f:
        data = ujson.load(f)

    fix_foreign_keys(data, 'zerver_realm', 'notifications_stream')
    fix_datetime_fields(data, 'zerver_realm', 'date_created')
    realm = Realm(**data['zerver_realm'][0])
    if realm.notifications_stream_id is not None:
        notifications_stream_id = int(realm.notifications_stream_id)
    else:
        notifications_stream_id = None
    realm.notifications_stream_id = None
    realm.save()
    bulk_import_client(data, Client, 'zerver_client')

    # Email tokens will automatically be randomly generated when the
    # Stream objects are created by Django.
    fix_datetime_fields(data, 'zerver_stream', 'date_created')
    fix_foreign_keys(data, 'zerver_stream', 'realm')
    bulk_import_model(data, Stream, 'zerver_stream')

    realm.notifications_stream_id = notifications_stream_id
    realm.save()

    fix_foreign_keys(data, "zerver_defaultstream", 'stream')
    for (table, model) in realm_tables:
        fix_foreign_keys(data, table, 'realm')
        bulk_import_model(data, model, table)

    # Remap the user IDs for notification_bot and friends to their
    # appropriate IDs on this server
    for item in data['zerver_userprofile_crossrealm']:
        logging.info("Adding to ID map: %s %s" % (item['id'], get_user_profile_by_email(item['email']).id))
        id_maps["user_profile"][item['id']] = get_user_profile_by_email(item['email']).id
    fix_datetime_fields(data, 'zerver_userprofile', 'date_joined')
    fix_datetime_fields(data, 'zerver_userprofile', 'last_login')
    fix_datetime_fields(data, 'zerver_userprofile', 'last_reminder')
    fix_foreign_keys(data, 'zerver_userprofile', 'realm')
    fix_foreign_keys(data, 'zerver_userprofile', 'bot_owner', id_map_table="user_profile")
    fix_foreign_keys(data, 'zerver_userprofile', 'default_sending_stream')
    fix_foreign_keys(data, 'zerver_userprofile', 'default_events_register_stream')
    for user_profile_dict in data['zerver_userprofile']:
        user_profile_dict['password'] = None
        user_profile_dict['api_key'] = random_api_key()
        # Since Zulip doesn't use these permissions, drop them
        del user_profile_dict['user_permissions']
        del user_profile_dict['groups']
    user_profiles = [UserProfile(**item) for item in data['zerver_userprofile']]
    for user_profile in user_profiles:
        user_profile.set_unusable_password()
    UserProfile.objects.bulk_create(user_profiles)

    if 'zerver_huddle' in data:
        bulk_import_model(data, Huddle, 'zerver_huddle')

    bulk_import_model(data, Recipient, 'zerver_recipient')
    fix_foreign_keys(data, 'zerver_subscription', 'user_profile', id_map_table="user_profile")
    fix_foreign_keys(data, 'zerver_subscription', 'recipient')
    bulk_import_model(data, Subscription, 'zerver_subscription')

    fix_datetime_fields(data, 'zerver_userpresence', 'timestamp')
    fix_foreign_keys(data, 'zerver_userpresence', 'user_profile', id_map_table="user_profile")
    fix_foreign_keys(data, 'zerver_userpresence', 'client', id_map_table='client')
    bulk_import_model(data, UserPresence, 'zerver_userpresence')

    fix_datetime_fields(data, 'zerver_useractivity', 'last_visit')
    fix_foreign_keys(data, 'zerver_useractivity', 'user_profile', id_map_table="user_profile")
    fix_foreign_keys(data, 'zerver_useractivity', 'client', id_map_table='client')
    bulk_import_model(data, UserActivity, 'zerver_useractivity')

    fix_datetime_fields(data, 'zerver_useractivityinterval', 'start')
    fix_datetime_fields(data, 'zerver_useractivityinterval', 'end')
    fix_foreign_keys(data, 'zerver_useractivityinterval', 'user_profile', id_map_table="user_profile")
    bulk_import_model(data, UserActivityInterval, 'zerver_useractivityinterval')

    # Import uploaded files and avatars
    import_uploads(os.path.join(import_dir, "avatars"), avatar_bucket=True)
    import_uploads(os.path.join(import_dir, "uploads"))

    dump_file_id = 1
    while True:
        message_filename = os.path.join(import_dir, "messages-%06d.json" % (dump_file_id,))
        if not os.path.exists(message_filename):
            break

        with open(message_filename) as f:
            data = ujson.load(f)

        logging.info("Importing message dump %s" % (message_filename,))
        fix_foreign_keys(data, 'zerver_message', 'sender', id_map_table="user_profile")
        fix_foreign_keys(data, 'zerver_message', 'recipient')
        fix_foreign_keys(data, 'zerver_message', 'sending_client', id_map_table='client')
        fix_datetime_fields(data, 'zerver_message', 'pub_date')
        fix_datetime_fields(data, 'zerver_message', 'last_edit_time')
        bulk_import_model(data, Message, 'zerver_message')

        # Due to the structure of these message chunks, we're
        # guaranteed to have already imported all the Message objects
        # for this batch of UserMessage objects.
        fix_foreign_keys(data, 'zerver_usermessage', 'message')
        fix_foreign_keys(data, 'zerver_usermessage', 'user_profile', id_map_table="user_profile")
        fix_bitfield_keys(data, 'zerver_usermessage', 'flags')
        bulk_import_model(data, UserMessage, 'zerver_usermessage')

        dump_file_id += 1

    fix_datetime_fields(data, 'zerver_attachment', 'create_time')
    fix_foreign_keys(data, 'zerver_attachment', 'owner', id_map_table="user_profile")
    fix_foreign_keys(data, 'zerver_attachment', 'realm')
    # TODO: Handle the `messages` keys.
    # fix_foreign_keys(data, 'zerver_attachment', 'messages')
    bulk_import_model(data, Attachment, 'zerver_attachment')
