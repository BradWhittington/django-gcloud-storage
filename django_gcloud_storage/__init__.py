# -*- encoding: utf-8 -*-
from __future__ import unicode_literals

import datetime
import os
import re
from tempfile import SpooledTemporaryFile

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible
from django.utils.encoding import force_text, smart_str
from gcloud import _helpers as gcloud_helpers
from gcloud import storage
from gcloud.exceptions import NotFound
from gcloud.storage.bucket import Bucket

try:
    # For Python 3.0 and later
    from urllib import parse as urlparse
except ImportError:
    # Fall back to Python 2's urllib2
    import urlparse


def safe_join(base, path):
    base = "/" + force_text(base).lstrip("/").rstrip("/") + "/"
    path = force_text(path).lstrip("/")

    # Ugh... there must be a better way that I can't think of right now
    if base == "//":
        base = "/"

    resolved_url = urlparse.urljoin(base, path)

    resolved_url = re.sub("//+", "/", resolved_url)

    if not resolved_url.startswith(base):
        raise SuspiciousFileOperation(
            'The joined path ({}) is located outside of the base path '
            'component ({})'.format(resolved_url, base))

    return resolved_url


def prepare_name(name):
    return smart_str(name, encoding='utf-8')


def remove_prefix(target, prefix):
    if target.startswith(prefix):
        return target[len(prefix):]
    return target


class GCloudFile(File):
    """
    Django file object that wraps a SpooledTemporaryFile and remembers changes on
    write to reupload the file to GCS on close()
    """

    def __init__(self, blob, maxsize=1000):
        """
        :type blob: gcloud.storage.blob.Blob
        """
        self._dirty = False
        self._tmpfile = SpooledTemporaryFile(
            max_size=maxsize,
            prefix="django_gcloud_storage_"
        )

        self._blob = blob

        super(GCloudFile, self).__init__(self._tmpfile)

    def _update_blob(self):
        self._blob.upload_from_file(self._tmpfile, rewind=True)

    def write(self, content):
        self._dirty = True
        super(GCloudFile, self).write(content)

    def close(self):
        if self._dirty:
            self._blob.upload_from_file(self._tmpfile, rewind=True)
            self._dirty = False

        super(GCloudFile, self).close()


# noinspection PyAbstractClass
@deconstructible
class DjangoGCloudStorage(Storage):

    def __init__(self, project, bucket, credentials_file_path):
        self._client = None
        self._bucket = None

        assert isinstance(bucket, str) or isinstance(bucket, unicode), "Bucket must be a string"
        assert bucket != "", "Bucket can't be empty"

        self.bucket_name = bucket

        assert os.path.exists(credentials_file_path), "Credentials file not found"

        self.credentials_file_path = credentials_file_path

        assert isinstance(project, str) or isinstance(bucket, unicode), "Project must be a string"
        assert project != "", "Project can't be empty"

        self.project_name = project

        self.bucket_subdir = ''  # TODO should be a parameter

    @property
    def client(self):
        """
        :rtype: storage.Client
        """
        if not self._client:
            self._client = storage.Client.from_service_account_json(
                self.credentials_file_path,
                project=self.project_name
            )
        return self._client

    @property
    def bucket(self):
        """
        :rtype: Bucket
        """
        if not self._bucket:
            self._bucket = self.client.get_bucket(self.bucket_name)
        return self._bucket

    def _save(self, name, content):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)


        blob = self.bucket.blob(name)
        blob.upload_from_file(content)

        return name

    def _open(self, name, mode):
        # TODO implement mode?

        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        blob = self.bucket.get_blob(name)
        tmpfile = GCloudFile(blob)
        blob.download_to_file(tmpfile)
        tmpfile.seek(0)

        return tmpfile

    def created_time(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        blob = self.bucket.get_blob(name)

        # gcloud doesn't provide a public method for this
        value = blob._properties.get("timeCreated", None)
        if value is not None:
            naive = datetime.datetime.strptime(value, gcloud_helpers._RFC3339_MICROS)
            return naive.replace(tzinfo=gcloud_helpers.UTC)

    def delete(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        try:
            self.bucket.delete_blob(name)
        except NotFound:
            pass

    def exists(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        return self.bucket.get_blob(name) is not None

    def size(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        blob = self.bucket.get_blob(name)

        return blob.size if blob is not None else None

    def modified_time(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        blob = self.bucket.get_blob(name)

        return blob.updated if blob is not None else None

    def listdir(self, path):
        path = safe_join(self.bucket_subdir, path)
        path = prepare_name(path)

        iterator = self.bucket.list_blobs(
            prefix=path,
            delimiter="/"
        )

        items = [remove_prefix(blob.name, path) for blob in list(iterator)]
        # prefixes is only set after first iterating the results!
        dirs = [remove_prefix(prefix, path).rstrip("/") for prefix in list(iterator.prefixes)]

        items.sort()
        dirs.sort()

        return dirs, items

    def url(self, name):
        name = safe_join(self.bucket_subdir, name)
        name = prepare_name(name)

        return self.bucket.get_blob(name).generate_signed_url(expiration=datetime.datetime.now() + datetime.timedelta(hours=1))
