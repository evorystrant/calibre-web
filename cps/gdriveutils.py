try:
    from pydrive.auth import GoogleAuth
    from pydrive.drive import GoogleDrive
    from apiclient import errors
except ImportError:
    pass
import os

from ub import config
import cli

from sqlalchemy import *
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import *

import web

engine = create_engine('sqlite:///{0}'.format(cli.gdpath), echo=False)
Base = declarative_base()

# Open session for database connection
Session = sessionmaker()
Session.configure(bind=engine)
session = scoped_session(Session)


class GdriveId(Base):
    __tablename__ = 'gdrive_ids'

    id = Column(Integer, primary_key=True)
    gdrive_id = Column(Integer, unique=True)
    path = Column(String)
    __table_args__ = (UniqueConstraint('gdrive_id', 'path', name='_gdrive_path_uc'),)

    def __repr__(self):
        return str(self.path)


class PermissionAdded(Base):
    __tablename__ = 'permissions_added'

    id = Column(Integer, primary_key=True)
    gdrive_id = Column(Integer, unique=True)

    def __repr__(self):
        return str(self.gdrive_id)


def migrate():
    if not engine.dialect.has_table(engine.connect(), "permissions_added"):
        PermissionAdded.__table__.create(bind=engine)
    for new_sql in session.execute("select sql from sqlite_master where type='table'"):
        if 'CREATE TABLE gdrive_ids' in new_sql[0]:
            curr_unique_constraint = 'UNIQUE (gdrive_id)'
            if curr_unique_constraint in new_sql[0]:
                new_sql = new_sql[0].replace(curr_unique_constraint, 'UNIQUE (gdrive_id, path)')
                new_sql = new_sql.replace(GdriveId.__tablename__, GdriveId.__tablename__ + '2')
                session.execute(new_sql)
                session.execute('INSERT INTO gdrive_ids2 (id, gdrive_id, path) '
                                'SELECT id, gdrive_id, path FROM gdrive_ids;')
                session.commit()
                session.execute('DROP TABLE %s' % 'gdrive_ids')
                session.execute('ALTER TABLE gdrive_ids2 RENAME to gdrive_ids')
            break


if not os.path.exists(cli.gdpath):
    try:
        Base.metadata.create_all(engine)
    except Exception:
        raise

migrate()


def get_drive(drive=None, gauth=None):
    if not drive:
        if not gauth:
            gauth = GoogleAuth(settings_file='settings.yaml')
        # Try to load saved client credentials
        gauth.LoadCredentialsFile("gdrive_credentials")
        if gauth.access_token_expired:
            # Refresh them if expired
            gauth.Refresh()
        else:
            # Initialize the saved creds
            gauth.Authorize()
        # Save the current credentials to a file
        return GoogleDrive(gauth)
    if drive.auth.access_token_expired:
        drive.auth.Refresh()
    return drive


def get_ebooks_folder(drive=None):
    drive = get_drive(drive)
    ebooks_folder = "title = '%s' and 'root' in parents and mimeType = 'application/vnd.google-apps.folder' " \
                    "and trashed = false" % config.config_google_drive_folder

    file_list = drive.ListFile({'q': ebooks_folder}).GetList()
    return file_list[0]


def get_ebooks_folder_id(drive=None):
    stored_path_name = session.query(GdriveId).filter(GdriveId.path == '/').first()
    if stored_path_name:
        return stored_path_name.gdrive_id
    else:
        g_drive_id = GdriveId()
        g_drive_id.gdrive_id = get_ebooks_folder(drive)['id']
        g_drive_id.path = '/'
        session.merge(g_drive_id)
        session.commit()
        return


def get_folder_in_folder(parent_id, folder_name, drive=None):
    drive = get_drive(drive)
    folder = "title = '%s' and '%s' in parents and mimeType = 'application/vnd.google-apps.folder' " \
             "and trashed = false" % (folder_name.replace("'", "\\'"), parent_id)
    file_list = drive.ListFile({'q': folder}).GetList()
    return file_list[0]


def get_file(path_id, file_name, drive=None):
    drive = get_drive(drive)
    meta_data_file = "'%s' in parents and trashed = false and title = '%s'" % (path_id, file_name.replace("'", "\\'"))

    file_list = drive.ListFile({'q': meta_data_file}).GetList()
    return file_list[0]


def get_folder_id(path, drive=None):
    drive = get_drive(drive)
    current_folder_id = get_ebooks_folder_id(drive)
    sql_check_path = path if path[-1] == '/' else path + '/'
    stored_path_name = session.query(GdriveId).filter(GdriveId.path == sql_check_path).first()

    if not stored_path_name:
        db_change = False
        s = path.split('/')
        for i, x in enumerate(s):
            if len(x) > 0:
                current_path = "/".join(s[:i + 1])
                if current_path[-1] != '/':
                    current_path = current_path + '/'
                stored_path_name = session.query(GdriveId).filter(GdriveId.path == current_path).first()
                if stored_path_name:
                    current_folder_id = stored_path_name.gdrive_id
                else:
                    current_folder_id = get_folder_in_folder(current_folder_id, x, drive)['id']
                    g_drive_id = GdriveId()
                    g_drive_id.gdrive_id = current_folder_id
                    g_drive_id.path = current_path
                    session.merge(g_drive_id)
                    db_change = True
        if db_change:
            session.commit()
    else:
        current_folder_id = stored_path_name.gdrive_id
    return current_folder_id


def get_file_from_ebooks_folder(drive, path, file_name):
    drive = get_drive(drive)
    if path:
        # sqlCheckPath=path if path[-1] =='/' else path + '/'
        folder_id = get_folder_id(path, drive)
    else:
        folder_id = get_ebooks_folder_id(drive)

    return get_file(folder_id, file_name, drive)


def copy_drive_file_remote(drive, origin_file_id, copy_title):
    drive = get_drive(drive)
    copied_file = {'title': copy_title}
    try:
        file_data = drive.auth.service.files().copy(
            fileId=origin_file_id, body=copied_file).execute()
        return drive.CreateFile({'id': file_data['id']})
    except errors.HttpError as error:
        print ('An error occurred: %s' % error)
    return None


def download_file(drive, path, filename, output):
    drive = get_drive(drive)
    f = get_file_from_ebooks_folder(drive, path, filename)
    f.GetContentFile(output)


def backup_calibre_db_and_optional_download(drive, f=None):
    drive = get_drive(drive)
    meta_data_file = "'%s' in parents and title = 'metadata.db' and trashed = false" % get_ebooks_folder_id()

    file_list = drive.ListFile({'q': meta_data_file}).GetList()

    database_file = file_list[0]

    if f:
        database_file.GetContentFile(f)


def copy_to_drive(drive, upload_file, create_root, replace_files,
                  ignore_files=None, parent=None, prev_dir=''):
    ignore_files = ignore_files or []
    drive = get_drive(drive)
    is_initial = not bool(parent)
    if not parent:
        parent = get_ebooks_folder(drive)
    if os.path.isdir(os.path.join(prev_dir, upload_file)):
        existing_folder = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" % (
            os.path.basename(upload_file), parent['id'])}).GetList()
        if len(existing_folder) == 0 and (not is_initial or create_root):
            parent = drive.CreateFile(
                {'title': os.path.basename(upload_file), 'parents': [{"kind": "drive#fileLink", 'id': parent['id']}],
                 "mimeType": "application/vnd.google-apps.folder"})
            parent.Upload()
        else:
            if (not is_initial or create_root) and len(existing_folder) > 0:
                parent = existing_folder[0]
        for f in os.listdir(os.path.join(prev_dir, upload_file)):
            if f not in ignore_files:
                copy_to_drive(drive, f, True, replace_files, ignore_files, parent, os.path.join(prev_dir, upload_file))
    else:
        if os.path.basename(upload_file) not in ignore_files:
            existing_files = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" % (
                os.path.basename(upload_file), parent['id'])}).GetList()
            if len(existing_files) > 0:
                drive_file = existing_files[0]
            else:
                drive_file = drive.CreateFile({'title': os.path.basename(upload_file),
                                               'parents': [{"kind": "drive#fileLink", 'id': parent['id']}], })
            drive_file.SetContentFile(os.path.join(prev_dir, upload_file))
            drive_file.Upload()


def upload_file_to_ebooks_folder(drive, dest_file, f):
    drive = get_drive(drive)
    parent = get_ebooks_folder(drive)
    split_dir = dest_file.split('/')
    for i, x in enumerate(split_dir):
        if i == len(split_dir) - 1:
            existing_files = drive.ListFile(
                {'q': "title = '%s' and '%s' in parents and trashed = false" % (x, parent['id'])}).GetList()
            if len(existing_files) > 0:
                drive_file = existing_files[0]
            else:
                drive_file = drive.CreateFile(
                    {'title': x, 'parents': [{"kind": "drive#fileLink", 'id': parent['id']}], })
            drive_file.SetContentFile(f)
            drive_file.Upload()
        else:
            existing_folder = drive.ListFile(
                {'q': "title = '%s' and '%s' in parents and trashed = false" % (x, parent['id'])}).GetList()
            if len(existing_folder) == 0:
                parent = drive.CreateFile({'title': x, 'parents': [{"kind": "drive#fileLink", 'id': parent['id']}],
                                           "mimeType": "application/vnd.google-apps.folder"})
                parent.Upload()
            else:
                parent = existing_folder[0]


def watch_change(drive, channel_id, channel_type, channel_address,
                 channel_token=None, expiration=None):
    drive = get_drive(drive)
    # Watch for all changes to a user's Drive.
    # Args:
    # service: Drive API service instance.
    # channel_id: Unique string that identifies this channel.
    # channel_type: Type of delivery mechanism used for this channel.
    # channel_address: Address where notifications are delivered.
    # channel_token: An arbitrary string delivered to the target address with
    #               each notification delivered over this channel. Optional.
    # channel_address: Address where notifications are delivered. Optional.
    # Returns:
    # The created channel if successful
    # Raises:
    # apiclient.errors.HttpError: if http request to create channel fails.
    body = {
        'id': channel_id,
        'type': channel_type,
        'address': channel_address
    }
    if channel_token:
        body['token'] = channel_token
    if expiration:
        body['expiration'] = expiration
    return drive.auth.service.changes().watch(body=body).execute()


def watch_file(drive, file_id, channel_id, channel_type, channel_address,
               channel_token=None, expiration=None):
    """Watch for any changes to a specific file.
    Args:
    service: Drive API service instance.
    file_id: ID of the file to watch.
    channel_id: Unique string that identifies this channel.
    channel_type: Type of delivery mechanism used for this channel.
    channel_address: Address where notifications are delivered.
    channel_token: An arbitrary string delivered to the target address with
                   each notification delivered over this channel. Optional.
    channel_address: Address where notifications are delivered. Optional.
    Returns:
    The created channel if successful
    Raises:
    apiclient.errors.HttpError: if http request to create channel fails.
    """
    drive = get_drive(drive)

    body = {
        'id': channel_id,
        'type': channel_type,
        'address': channel_address
    }
    if channel_token:
        body['token'] = channel_token
    if expiration:
        body['expiration'] = expiration
    return drive.auth.service.files().watch(fileId=file_id, body=body).execute()


def stop_channel(drive, channel_id, resource_id):
    """Stop watching to a specific channel.
    Args:
    service: Drive API service instance.
    channel_id: ID of the channel to stop.
    resource_id: Resource ID of the channel to stop.
    Raises:
    apiclient.errors.HttpError: if http request to create channel fails.
    """
    drive = get_drive(drive)
    # service=drive.auth.service
    body = {
        'id': channel_id,
        'resourceId': resource_id
    }
    return drive.auth.service.channels().stop(body=body).execute()


def get_change_by_id(drive, change_id):
    drive = get_drive(drive)
    # Print a single Change resource information.
    #
    # Args:
    # service: Drive API service instance.
    # change_id: ID of the Change resource to retrieve.
    try:
        change = drive.auth.service.changes().get(changeId=change_id).execute()
        return change
    except (errors.HttpError, error):
        web.app.logger.exception(error)
        return None
