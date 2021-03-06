#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os

from django.contrib.auth.models import User
from django.core import management
from django.core.management.base import NoArgsCommand
from django.db import transaction
from django.utils.translation import ugettext as _
from desktop.conf import DEFAULT_USER
from useradmin.management.commands.create_sandbox_user import Command as CreateSandboxUserCommand
from hadoop import cluster

from oozie.conf import LOCAL_SAMPLE_DATA_DIR, LOCAL_SAMPLE_DIR,\
  REMOTE_SAMPLE_DIR
from liboozie.submittion import create_directories


LOG = logging.getLogger(__name__)


class Command(NoArgsCommand):
  def handle_noargs(self, **options):
    fs = cluster.get_hdfs()
    sample_user = CreateSandboxUserCommand().handle_noargs()
    fs.setuser(sample_user)
    create_directories(fs, [REMOTE_SAMPLE_DIR.get()])
    remote_dir = REMOTE_SAMPLE_DIR.get()
    # Copy examples binaries
    for name in os.listdir(LOCAL_SAMPLE_DIR.get()):
      local_dir = fs.join(LOCAL_SAMPLE_DIR.get(), name)
      remote_data_dir = fs.join(remote_dir, name)
      LOG.info(_('Copying examples %(local_dir)s to %(remote_data_dir)s\n') % {
                  'local_dir': local_dir, 'remote_data_dir': remote_data_dir})
      fs.copyFromLocal(local_dir, remote_data_dir)

    # Copy sample data
    local_dir = LOCAL_SAMPLE_DATA_DIR.get()
    remote_data_dir = fs.join(remote_dir, 'data')
    LOG.info(_('Copying data %(local_dir)s to %(remote_data_dir)s\n') % {
                'local_dir': local_dir, 'remote_data_dir': remote_data_dir})
    fs.copyFromLocal(local_dir, remote_data_dir)

    # Load jobs

    management.call_command('loaddata', 'initial_oozie_examples.json', verbosity=2)
