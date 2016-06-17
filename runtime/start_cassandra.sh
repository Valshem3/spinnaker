#!/bin/bash
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

CASSANDRA_DIR=${CASSANDRA_DIR:-"$(dirname $0)/../cassandra"}
CASSANDRA_PORT=${CASSANDRA_PORT:-9042}
CASSANDRA_HOST=${CASSANDRA_HOST:-127.0.0.1}
export CQLSH_HOST=$CASSANDRA_HOST

function is_local() {
  local ip="$1"
  if [[ "$ip" == "localhost" || "$ip" == "$(hostname)" || "$ip" == "0.0.0.0" ]]; then
      return 0
  elif ifconfig | grep " inet addr:${ip} "  > /dev/null; then
      return 0
  else
      return 1
  fi
}

function maybe_start_cassandra() {
  if is_local "$CASSANDRA_HOST"; then
    echo "Starting Cassandra on $CASSANDRA_HOST"
    sudo service cassandra start
  else
    echo "Using remote Cassandra from $CASSANDRA_HOST"
  fi
}

echo "Checking for Cassandra on $CASSANDRA_HOST:$CASSANDRA_PORT"
if nc -z $CASSANDRA_HOST $CASSANDRA_PORT; then
  echo "Cassandra is already up on $CASSANDRA_HOST:$CASSANDRA_PORT."
else
  maybe_start_cassandra
  echo "Waiting for Cassandra to start accepting requests on" \
       "$CASSANDRA_HOST:$CASSANDRA_PORT..."
  while ! nc -z $CASSANDRA_HOST $CASSANDRA_PORT; do sleep 0.1; done;
  echo "Cassandra is up."
fi


# Create Cassandra keyspaces.
echo "Creating Cassandra keyspaces..."
DELAY=1
while ! cqlsh -f "$CASSANDRA_DIR/create_echo_keyspace.cql" && [ "$DELAY" -lt 32 ]
do
    sleep $DELAY
    let DELAY*=2
done

cqlsh -f "$CASSANDRA_DIR/create_front50_keyspace.cql"

echo "Cassandra is ready."
