# Copyright 2017 Google Inc. All Rights Reserved.
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

"""Common helper functions across buildtool modules."""

import datetime
import logging
import os
import shlex
import subprocess
import time
import traceback


def timestring(now=None):
  """Returns timestamp as date time string."""
  now = now or datetime.datetime.now()
  return '{:%Y-%m-%d %H:%M:%S}'.format(now)


def timedelta_string(delta):
  """Returns a string indicating the time duration.

  Args:
    delta: [datetime.timedelta] The time difference
  """
  delta_secs = int(delta.total_seconds())
  delta_mins = delta_secs / 60
  delta_hours = (delta_mins / 60 % 24)
  delta_days = delta.days

  day_str = '' if not delta_days else ('days=%d + ' % delta_days)
  delta_mins = delta_mins % 60
  delta_secs = delta_secs % 60

  if delta_hours or day_str:
    return day_str + '%02d:%02d:%02d' % (delta_hours, delta_mins, delta_secs)
  elif delta_mins:
    return '%02d:%02d' % (delta_mins, delta_secs)
  return '%d.%03d secs' % (delta_secs, delta.microseconds / 1000)


def maybe_log_exception(where, ex, action_msg='propagating exception'):
  """Log the exception and stackdrace if it hasnt been logged already."""
  if not hasattr(ex, 'logged'):
    text = traceback.format_exc()
    logging.error('"%s" caught exception\n%s', where, text)
    ex.logged = True
  logging.error('"%s" %s', where, action_msg)


def run_subprocess(cmd, stream=None, echo=False, **kwargs):
  """Returns retcode, stdout."""
  split_cmd = shlex.split(cmd)

  if echo:
    logging.info('Running %s ...', cmd)
  else:
    logging.debug('Running %s ...', cmd)

  start_date = datetime.datetime.now()
  if stream:
    stream.write('{time} Spawning {cmd}\n----\n\n'.format(
        time=timestring(now=start_date), cmd=cmd))
    stream.flush()

  process = subprocess.Popen(
      split_cmd,
      close_fds=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      **kwargs)
  time.sleep(0) # yield this thread

  stdout_lines = []
  for line in iter(process.stdout.readline, ''):
    stdout_lines.append(line)
    if stream:
      stream.write(line)
      stream.flush()
  process.wait()
  end_date = datetime.datetime.now()
  delta_time_str = timedelta_string(end_date - start_date)
  returncode = process.returncode
  stdout = ''.join(stdout_lines)

  if stream:
    stream.write(
        '\n\n----\n{time} Spawned process completed'
        ' with returncode {returncode} in {delta_time}.\n'
        .format(time=timestring(now=end_date), returncode=returncode,
                delta_time=delta_time_str))
    stream.flush()

  if echo:
    logging.info('%s returned %d with output:\n%s',
                 split_cmd[0], returncode, stdout)
  logging.debug('Finished %s with returncode=%d in %s',
                split_cmd[0], returncode, delta_time_str)

  return returncode, stdout.strip()


def check_subprocess(cmd, stream=None, **kwargs):
  """run_subprocess and raise CalledProcessError if it fails."""
  retcode, stdout = run_subprocess(cmd, stream=stream, **kwargs)

  try:
    if retcode != 0:
      lines = stdout.split('\n')
      if lines > 5:
        lines = lines[-5:]
      logging.error('Command failed with last %d lines:\n   %s',
                    len(lines), '\n   '.join(lines))
      raise subprocess.CalledProcessError(retcode, cmd, output=stdout)
  except subprocess.CalledProcessError as ex:
    maybe_log_exception(shlex.split(cmd)[0], ex)
    raise

  return stdout.strip()


def check_subprocess_sequence(cmd_list, stream=None, **kwargs):
  """Run multiple commands until one fails.

  Returns:
    A list of each result in sequence if all succeeded.
  """
  response = []
  for one in cmd_list:
    response.append(check_subprocess(one, stream=stream, **kwargs))
  return response


def run_subprocess_sequence(cmd_list, stream=None, **kwargs):
  """Run multiple commands until one fails.

  Returns:
    A list of (code, output) tuples for each result in sequence.
  """
  response = []
  for one in cmd_list:
    response.append(run_subprocess(one, stream=stream, **kwargs))
  return response


def ensure_dir_exists(path):
  """Ensure a directory exists, creating it if not."""
  try:
    os.makedirs(path)
  except OSError:
    if not os.path.exists(path):
      raise


def write_to_path(content, path):
  """Write the given content to the file specified by the <path>.

  This will create the parent directory if needed.
  """
  ensure_dir_exists(os.path.dirname(os.path.abspath(path)))
  with open(path, 'w') as f:
    f.write(content)
