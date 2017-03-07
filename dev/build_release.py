#!/usr/bin/python
#
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Coordinates a global build of a Spinnaker "release".

The term "release" here is more of an encapsulated build. This is not
an official release. It is meant for developers.

The gradle script does not yet coordinate a complete build, so
this script fills that gap for the time being. It triggers all
the subsystem builds and then publishes the resulting artifacts.

Publishing is typically to bintray. It is currently possible to publish
to a filesystem or storage bucket but this option will be removed soon
since the installation from these sources is no longer supported.

Usage:
  export BINTRAY_USER=
  export BINTRAY_KEY=

  # subject/repository are the specific bintray repository
  # owner and name components that specify the repository you are updating.
  # The repository must already exist, but can be empty.
  BINTRAY_REPOSITORY=subject/repository

  # cd <build root containing subsystem subdirectories>
  # this is where you ran refresh_source.sh from

  ./spinnaker/dev/build_release.sh --bintray_repo=$BINTRAY_REPOSITORY
"""

import argparse
import base64
import collections
import glob
import os
import multiprocessing
import multiprocessing.pool
import re
import shutil
import subprocess
import sys
import tempfile
import urllib2
import zipfile
from urllib2 import HTTPError

import refresh_source
from spinnaker.run import check_run_quick
from spinnaker.run import run_quick


SUBSYSTEM_LIST = ['clouddriver', 'orca', 'front50', 'halyard',
                  'echo', 'rosco', 'gate', 'igor', 'fiat', 'deck', 'spinnaker']
ADDITIONAL_SUBSYSTEMS = ['spinnaker-monitoring']

def ensure_gcs_bucket(name, project=''):
  """Ensure that the desired GCS bucket exists, creating it if needed.

  Args:
    name [string]: The bucket name.
    project [string]: Optional Google Project id that will own the bucket.
      If none is provided, then the bucket will be associated with the default
      bucket configured to gcloud.

  Raises:
    RuntimeError if the bucket could not be created.
  """
  bucket = 'gs://'+ name
  if not project:
      config_result = run_quick('gcloud config list', echo=False)
      error = None
      if config_result.returncode:
        error = 'Could not run gcloud: {error}'.format(
            error=config_result.stdout)
      else:
        match = re.search('(?m)^project = (.*)', config_result.stdout)
        if not match:
          error = ('gcloud is not configured with a default project.\n'
                   'run gcloud config or provide a --google_project.\n')
      if error:
        raise SystemError(error)

      project = match.group(1)

  list_result = run_quick('gsutil list -p ' +  project, echo=False)
  if list_result.returncode:
    error = ('Could not create Google Cloud Storage bucket'
             '"{name}" in project "{project}":\n{error}'
             .format(name=name, project=project, error=list_result.stdout))
    raise RuntimeError(error)

  if re.search('(?m)^{bucket}/\n'.format(bucket=bucket), list_result.stdout):
    sys.stderr.write(
        'WARNING: "{bucket}" already exists. Overwriting.\n'.format(
        bucket=bucket))
  else:
    print 'Creating GCS bucket "{bucket}" in project "{project}".'.format(
        bucket=bucket, project=project)
    check_run_quick('gsutil mb -p {project} {bucket}'
                    .format(project=project, bucket=bucket),
                    echo=True)


def ensure_s3_bucket(name, region=""):
  """Ensure that the desired S3 bucket exists, creating it if needed.

  Args:
    name [string]: The bucket name.
    region [string]: The S3 region for the bucket. If empty use aws default.

  Raises:
    RuntimeError if the bucket could not be created.
  """
  bucket = 's3://' + name
  list_result = run_quick('aws s3 ls ' + bucket, echo=False)
  if not list_result.returncode:
    sys.stderr.write(
        'WARNING: "{bucket}" already exists. Overwriting.\n'.format(
        bucket=bucket))
  else:
    print 'Creating S3 bucket "{bucket}"'.format(bucket=bucket)
    command = 'aws s3 mb ' + bucket
    if region:
      command += ' --region ' + region
    check_run_quick(command, echo=False)


class BackgroundProcess(
    collections.namedtuple('BackgroundProcess', ['name', 'subprocess'])):
  """Denotes a running background process.

  Attributes:
    name [string]: The visible name of the process for reporting.
    subprocess [subprocess]: The subprocess instance.
  """

  @staticmethod
  def spawn(name, args):
      sp = subprocess.Popen(args, shell=True, close_fds=True,
                            stdout=sys.stdout, stderr=subprocess.STDOUT)
      return BackgroundProcess(name, sp)

  def wait(self):
    if not self.subprocess:
      return None
    return self.subprocess.wait()

  def check_wait(self):
    if self.wait():
      error = '{name} failed.'.format(name=self.name)
      raise SystemError(error)


NO_PROCESS = BackgroundProcess('nop', None)

def determine_project_root():
  return os.path.abspath(os.path.dirname(__file__) + '/..')

def determine_modules_with_debians(gradle_root):
  files = glob.glob(os.path.join(gradle_root, '*', 'build', 'debian', 'control'))
  dirs = [os.path.dirname(os.path.dirname(os.path.dirname(file))) for file in files]
  if os.path.exists(os.path.join(gradle_root, 'build', 'debian', 'control')):
    dirs.append(gradle_root)
  return dirs

def determine_package_version(gradle_root):
  root = determine_modules_with_debians(gradle_root)

  with open(os.path.join(root[0], 'build', 'debian', 'control')) as f:
     content = f.read()
  match = re.search('(?m)^Version: (.*)', content)
  return match.group(1)


class Builder(object):
  """Knows how to coordinate a Spinnaker release."""

  def __init__(self, options):
      self.__package_list = []
      self.__build_failures = []
      self.__background_processes = []

      os.environ['NODE_ENV'] = os.environ.get('NODE_ENV', 'dev')
      self.__build_number = options.build_number
      self.__options = options
      self.refresher = refresh_source.Refresher(options)
      if options.bintray_repo:
        self.__verify_bintray()


      # NOTE(ewiseblatt):
      # This is the GCE directory.
      # Ultimately we'll want to go to the root directory and install
      # standard stuff and gce stuff.
      self.__project_dir = determine_project_root()
      self.__release_dir = options.release_path

      if self.__release_dir.startswith('gs://'):
          ensure_gcs_bucket(name=self.__release_dir[5:].split('/')[0],
                            project=options.google_project)
      elif self.__release_dir.startswith('s3://'):
          ensure_s3_bucket(name=self.__release_dir[5:].split('/')[0],
                           region=options.aws_region)

  def determine_gradle_root(self, name):
      gradle_root = (name if name != 'spinnaker'
              else os.path.join(self.__project_dir, 'experimental/buildDeb'))
      gradle_root = name if name != 'spinnaker' else self.__project_dir
      return gradle_root

  def start_subsystem_build(self, name):
    """Start a subprocess to build and publish the designated component.

    This function runs a gradle 'candidate' task using the last git tag as the
    package version and the Bintray configuration passed through arguments. The
    'candidate' task release builds the source, packages the debian and jar
    files, and publishes those to the respective Bintray '$org/$repository'.

    The naming of the gradle task is a bit unfortunate because of the
    terminology used in the Spinnaker product release process. The artifacts
    produced by this script are not 'release candidate' artifacts, they are
    pre-validation artifacts. Maybe we can modify the task name at some point.

    The gradle 'candidate' task throws a 409 if the package we are trying to
    publish already exists. We'll publish unique package versions using build
    numbers. These will be transparent to end users since the only meaningful
    version is the Spinnaker product version.

    We will use -Prelease.useLastTag=true and ensure the last git tag is the
    version we want to use. This tag has to be of the form 'X.Y.Z-$build' or
    'vX.Y.Z-$build for gradle to use the tag as the version. This script will
    assume that the source has been properly tagged to use the latest tag as the
    package version for each component.

    Args:
      name [string]: Name of the subsystem repository.

    Returns:
      BackgroundProcess
    """
    jarRepo = self.__options.jar_repo
    parts = self.__options.bintray_repo.split('/')
    if len(parts) != 2:
      raise ValueError(
          'Expected --bintray_repo to be in the form <owner>/<repo>')
    org, packageRepo = parts[0], parts[1]
    bintray_key = os.environ['BINTRAY_KEY']
    bintray_user = os.environ['BINTRAY_USER']

    if self.__options.nebula:
      target = 'candidate'
      extra_args = [
          '--stacktrace',
          '-Prelease.useLastTag=true',
          '-PbintrayPackageBuildNumber={number}'.format(
              number=self.__build_number),
          '-PbintrayOrg="{org}"'.format(org=org),
          '-PbintrayPackageRepo="{repo}"'.format(repo=packageRepo),
          '-PbintrayJarRepo="{jarRepo}"'.format(jarRepo=jarRepo),
          '-PbintrayKey="{key}"'.format(key=bintray_key),
          '-PbintrayUser="{user}"'.format(user=bintray_user)
        ]
    else:
      target = 'buildDeb'
      extra_args = []

    if name == 'deck' and not 'CHROME_BIN' in os.environ:
      extra_args.append('-PskipTests')

    # Currently spinnaker is in a separate location
    gradle_root = self.determine_gradle_root(name)
    print 'Building and publishing {name}...'.format(name=name)
    # Note: 'candidate' is just the gradle task name. It doesn't indicate
    # 'release candidate' status for the artifacts created through this build.
    return BackgroundProcess.spawn(
      'Building and publishing {name}...'.format(name=name),
      'cd "{gradle_root}"; ./gradlew {extra} {target}'.format(
          gradle_root=gradle_root, extra=' '.join(extra_args), target=target)
    )

  def publish_to_bintray(self, source, package, version, path, debian_tags=''):
    bintray_key = os.environ['BINTRAY_KEY']
    bintray_user = os.environ['BINTRAY_USER']
    parts = self.__options.bintray_repo.split('/')
    if len(parts) != 2:
      raise ValueError(
          'Expected --bintray_repo to be in the form <owner>/<repo>')
    subject, repo = parts[0], parts[1]

    deb_filename = os.path.basename(path)
    if (deb_filename.startswith('spinnaker-')
        and not package.startswith('spinnaker')):
      package = 'spinnaker-' + package

    if debian_tags and debian_tags[0] != ';':
      debian_tags = ';' + debian_tags

    url = ('https://api.bintray.com/content'
           '/{subject}/{repo}/{package}/{version}/{path}'
           '{debian_tags}'
           ';publish=1;override=1'
           .format(subject=subject, repo=repo, package=package,
                   version=version, path=path,
                   debian_tags=debian_tags))

    with open(source, 'r') as f:
        data = f.read()
        put_request = urllib2.Request(url)
        encoded_auth = base64.encodestring('{user}:{pwd}'.format(
            user=bintray_user, pwd=bintray_key))[:-1]  # strip eoln

        put_request.add_header('Authorization', 'Basic ' + encoded_auth)
        put_request.get_method = lambda: 'PUT'
        try:
            result = urllib2.urlopen(put_request, data)
        except HTTPError as put_error:
            if put_error.code == 409 and self.__options.wipe_package_on_409:
              # The problem here is that BinTray does not allow packages to change once
              # they have been published (even though we are explicitly asking it to
              # override). PATCH wont work either.
              # Since we are building from source, we don't really have a version
              # yet, since we are still modifying the code. Either we need to generate a new
              # version number every time or we don't want to publish these.
              # Ideally we could control whether or not to publish. However,
              # if we do not publish, then the repository will not be visible without
              # credentials, and adding conditional credentials into the packer scripts
              # starts getting even more complex.
              #
              # We cannot seem to delete individual versions either (at least not for
              # InstallSpinnaker.sh, which is where this problem seems to occur),
              # so we'll be heavy handed and wipe the entire package.
              print 'Got 409 on {url}.'.format(url=url)
              delete_url = ('https://api.bintray.com/content'
                            '/{subject}/{repo}/{path}'
                            .format(subject=subject, repo=repo, path=path))
              print 'Attempt to delete url={url} then retry...'.format(url=delete_url)
              delete_request = urllib2.Request(delete_url)
              delete_request.add_header('Authorization', 'Basic ' + encoded_auth)
              delete_request.get_method = lambda: 'DELETE'
              try:
                urllib2.urlopen(delete_request)
                print 'Deleted...'
              except HTTPError as ex:
                # Maybe it didn't exist. Try again anyway.
                print 'Delete {url} got {ex}. Try again anyway.'.format(url=url, ex=ex)
              print 'Retrying {url}'.format(url=url)
              result = urllib2.urlopen(put_request, data)
              print 'SUCCESS'

            elif put_error.code != 400:
              raise

            else:
              # Try creating the package and retrying.
              pkg_url = os.path.join('https://api.bintray.com/packages',
                                     subject, repo)
              print 'Creating an entry for {package} with {pkg_url}...'.format(
                  package=package, pkg_url=pkg_url)

              # All the packages are from spinnaker so we'll hardcode it.
              # Note spinnaker-monitoring is a github repo with two packages.
              # Neither is "spinnaker-monitoring"; that's only the github repo.
              gitname = (package.replace('spinnaker-', '')
                         if not package.startswith('spinnaker-monitoring')
                         else 'spinnaker-monitoring')
              pkg_data = """{{
                "name": "{package}",
                "licenses": ["Apache-2.0"],
                "vcs_url": "https://github.com/spinnaker/{gitname}.git",
                "website_url": "http://spinnaker.io",
                "github_repo": "spinnaker/{gitname}",
                "public_download_numbers": false,
                "public_stats": false
              }}'""".format(package=package, gitname=gitname)

              pkg_request = urllib2.Request(pkg_url)
              pkg_request.add_header('Authorization', 'Basic ' + encoded_auth)
              pkg_request.add_header('Content-Type', 'application/json')
              pkg_request.get_method = lambda: 'POST'
              pkg_result = urllib2.urlopen(pkg_request, pkg_data)
              pkg_code = pkg_result.getcode()
              if pkg_code >= 200 and pkg_code < 300:
                  result = urllib2.urlopen(put_request, data)

        code = result.getcode()
        if code < 200 or code >= 300:
          raise ValueError('{code}: Could not add version to {url}\n{msg}'
                           .format(code=code, url=url, msg=result.read()))

    print 'Wrote {source} to {url}'.format(source=source, url=url)

  def publish_install_script(self, source):
    gradle_root = self.determine_gradle_root('spinnaker')
    version = determine_package_version(gradle_root)

    self.publish_to_bintray(source, package='spinnaker', version=version,
                            path='InstallSpinnaker.sh')

  def publish_file(self, source, package, version):
    """Write a file to the bintray repository.

    Args:
      source [string]: The path to the source to copy must be local.
    """
    path = os.path.basename(source)
    debian_tags = ';'.join(['deb_component=spinnaker',
                            'deb_distribution=trusty,utopic,vivid,wily',
                            'deb_architecture=all'])

    self.publish_to_bintray(source, package=package, version=version,
                            path=path, debian_tags=debian_tags)


  def start_copy_file(self, source, target):
      """Start a subprocess to copy the source file.

      Args:
        source [string]: The path to the source to copy must be local.
        target [string]: The target path can also be a storage service URI.

      Returns:
        BackgroundProcess
      """
      if target.startswith('s3://'):
        return BackgroundProcess.spawn(
            'Copying {source}'.format,
            'aws s3 cp "{source}" "{target}"'
            .format(source=source, target=target))
      elif target.startswith('gs://'):
        return BackgroundProcess.spawn(
            'Copying {source}'.format,
            'gsutil -q -m cp "{source}" "{target}"'
            .format(source=source, target=target))
      else:
        try:
          os.makedirs(os.path.dirname(target))
        except OSError:
          pass

        shutil.copy(source, target)
        return NO_PROCESS

  def start_copy_debian_target(self, name):
      """Copies the debian package for the specified subsystem.

      Args:
        name [string]: The name of the subsystem repository.
      """
      pids = []
      gradle_root = self.determine_gradle_root(name)
      version = determine_package_version(gradle_root)
      for root in determine_modules_with_debians(gradle_root):
        deb_dir = '{root}/build/distributions'.format(root=root)

        non_spinnaker_name = '{name}_{version}_all.deb'.format(
              name=name, version=version)

        if os.path.exists(os.path.join(deb_dir,
                                       'spinnaker-' + non_spinnaker_name)):
         deb_file = 'spinnaker-' + non_spinnaker_name
        elif os.path.exists(os.path.join(deb_dir, non_spinnaker_name)):
          deb_file = non_spinnaker_name
        else:
          module_name = os.path.basename(
            os.path.dirname(os.path.dirname(deb_dir)))
          deb_file = '{module_name}_{version}_all.deb'.format(
            module_name=module_name, version=version)

        if not os.path.exists(os.path.join(deb_dir, deb_file)):
          error = ('.deb for name={name} version={version} is not in {dir}\n'
                   .format(name=name, version=version, dir=deb_dir))
          raise AssertionError(error)

        from_path = os.path.join(deb_dir, deb_file)
        print 'Adding {path}'.format(path=from_path)
        self.__package_list.append(from_path)
        basename = os.path.basename(from_path)
        module_name = basename[0:basename.find('_')]
        if self.__options.bintray_repo:
          self.publish_file(from_path, module_name, version)

        if self.__release_dir:
          to_path = os.path.join(self.__release_dir, deb_file)
          pids.append(self.start_copy_file(from_path, to_path))

      return pids

  def __do_build(self, subsys):
    try:
      self.start_subsystem_build(subsys).check_wait()
    except Exception as ex:
      self.__build_failures.append(subsys)

  def build_packages(self):
      """Build all the Spinnaker packages."""
      if self.__options.build:
        # Build in parallel using half available cores
        # to keep load in check.
        all_subsystems = []
        all_subsystems.extend(SUBSYSTEM_LIST)
        all_subsystems.extend(ADDITIONAL_SUBSYSTEMS)
        weighted_processes = self.__options.cpu_ratio * multiprocessing.cpu_count()
        pool = multiprocessing.pool.ThreadPool(
            processes=int(max(1, weighted_processes)))
        pool.map(self.__do_build, all_subsystems)

      if self.__build_failures:
        if set(self.__build_failures).intersection(set(SUBSYSTEM_LIST)):
          raise RuntimeError('Builds failed for {0!r}'.format(
            self.__build_failures))
        else:
          print 'Ignoring errors on optional subsystems {0!r}'.format(
              self.__build_failures)

      if self.__options.nebula:
        return

      wait_on = set(all_subsystems).difference(set(self.__build_failures))
      pool = multiprocessing.pool.ThreadPool(processes=len(wait_on))
      print 'Copying packages...'
      pool.map(self.__do_copy, wait_on)
      return

  def __do_copy(self, subsys):
    print 'Starting to copy {0}...'.format(subsys)
    pids = self.start_copy_debian_target(subsys)
    for p in pids:
      p.check_wait()
    print 'Finished copying {0}.'.format(subsys)

  @staticmethod
  def __zip_dir(zip_file, source_path, arcname=''):
    """Zip the contents of a directory.

    Args:
      zip_file: [ZipFile] The zip file to write into.
      source_path: [string] The directory to add.
      arcname: [string] Optional name for the source to appear as in the zip.
    """
    if arcname:
      # Effectively replace os.path.basename(parent_path) with arcname.
      arcbase = arcname + '/'
      parent_path = source_path
    else:
      # Will start relative paths from os.path.basename(source_path).
      arcbase = ''
      parent_path = os.path.dirname(source_path)

    # Copy the tree at source_path adding relative paths into the zip.
    rel_offset = len(parent_path) + 1
    entries = os.walk(source_path)
    for root, dirs, files in entries:
      for dirname in dirs:
        abs_path = os.path.join(root, dirname)
        zip_file.write(abs_path, arcbase + abs_path[rel_offset:])
      for filename in files:
        abs_path = os.path.join(root, filename)
        zip_file.write(abs_path, arcbase + abs_path[rel_offset:])

  @classmethod
  def init_argument_parser(cls, parser):
      refresh_source.Refresher.init_argument_parser(parser)
      parser.add_argument('--build', default=True, action='store_true',
                          help='Build the sources.')
      parser.add_argument(
        '--cpu_ratio', type=float, default=1.25,  # 125%
        help='Number of concurrent threads as ratio of available cores.')

      parser.add_argument('--nobuild', dest='build', action='store_false')
      config_path = os.path.join(determine_project_root(), 'config')

      parser.add_argument(
          '--config_source', default=config_path,
          help='Path to directory for release config file templates.')

      parser.add_argument('--release_path', default='',
                          help='Specifies the path to the release to build.'
                             ' The release name is assumed to be the basename.'
                             ' The path can be a directory, GCS URI or S3 URI.')
      parser.add_argument(
        '--google_project', default='',
        help='If release repository is a GCS bucket then this is the project'
        ' owning the bucket. The default is the project configured as the'
        ' default for gcloud.')

      parser.add_argument(
        '--aws_region', default='',
        help='If release repository is a S3 bucket then this is the AWS'
        ' region to add the bucket to if the bucket did not already exist.')

      parser.add_argument(
        '--bintray_repo', default='',
        help='Publish to this bintray repo.\n'
             'This requires BINTRAY_USER and BINTRAY_KEY are set.')
      parser.add_argument(
        '--jar_repo', default='',
        help='Publish produced jars to this repo.\n'
             'This requires BINTRAY_USER and BINTRAY_KEY are set.')
      parser.add_argument(
        '--build_number', default=os.environ.get('BUILD_NUMBER', ''),
        help='CI system build number. Ideally should be a unique integer'
        'for each build.')

      parser.add_argument(
        '--wipe_package_on_409', default=False, action='store_true',
        help='Work around BinTray conflict errors by deleting the entire package'
             ' and retrying. Removes all prior versions so only intended for dev'
             ' repos.\n')
      parser.add_argument(
        '--nowipe_package_on_409', dest='wipe_package_on_409',
        action='store_false')

      parser.add_argument(
        '--nebula', default=True, action='store_true',
        help='Use nebula to build "candidate" target and upload to bintray.')
      parser.add_argument(
        '--nonebula', dest='nebula', action='store_false',
        help='Explicitly "buildDeb" then curl upload them to bintray.')


  def __verify_bintray(self):
    if not os.environ.get('BINTRAY_KEY', None):
      raise ValueError('BINTRAY_KEY environment variable not defined')
    if not os.environ.get('BINTRAY_USER', None):
      raise ValueError('BINTRAY_USER environment variable not defined')


  @classmethod
  def main(cls):
    parser = argparse.ArgumentParser()
    cls.init_argument_parser(parser)
    options = parser.parse_args()

    if not (options.bintray_repo):
      sys.stderr.write('ERROR: Missing a --bintray_repo')
      return -1

    builder = cls(options)
    if options.pull_origin:
        builder.refresher.pull_all_from_origin()

    builder.build_packages()

    if options.bintray_repo:
      fd, temp_path = tempfile.mkstemp()
      with open(os.path.join(determine_project_root(), 'InstallSpinnaker.sh'),
                'r') as f:
          content = f.read()
          match = re.search(
                'REPOSITORY_URL="https://dl\.bintray\.com/(.+)"',
                content)
          content = ''.join([content[0:match.start(1)],
                             options.bintray_repo,
                             content[match.end(1):]])
          os.write(fd, content)
      os.close(fd)

      try:
        builder.publish_install_script(
          os.path.join(determine_project_root(), temp_path))
      finally:
        os.remove(temp_path)

      print '\nFINISHED writing release to {rep}'.format(
        rep=options.bintray_repo)


    if options.release_path:
      print '\nFINISHED writing release to {dir}'.format(
        dir=builder.__release_dir)

if __name__ == '__main__':
  sys.exit(Builder.main())
