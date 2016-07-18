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

"""
Views & controls for creating tables
"""

import errno
import stat
import logging
import gzip

from django.core.urlresolvers import reverse
from django.utils.translation import ugettext as _

from desktop.context_processors import get_app_name
from desktop.lib import django_mako, i18n
from desktop.lib.django_util import render
from desktop.lib.exceptions_renderable import PopupException
from desktop.lib.django_forms import MultiForm
from hadoop.fs import hadoopfs
from hadoop.conf import UPLOAD_CHUNK_SIZE

from beeswax.common import TERMINATORS
from beeswax.design import hql_query
from beeswax.forms import CreateTableForm, ColumnTypeFormSet,\
  PartitionTypeFormSet, CreateByImportFileForm, CreateByImportDelimForm,\
  TERMINATOR_CHOICES
from beeswax.server import dbms
from beeswax.views import execute_directly, _get_last_database


LOG = logging.getLogger(__name__)


def create_table(request, database=None):
  """Create a table by specifying its attributes manually"""
  database = _get_last_database(request, database)
  db = dbms.get(request.user)

  form = MultiForm(
      table=CreateTableForm,
      columns=ColumnTypeFormSet,
      partitions=PartitionTypeFormSet)

  if request.method == "POST":
    form.bind(request.POST)
    form.table.db = db  # curry is invalid

    if request.POST.get('create'):
      if form.is_valid():
        columns = [ f.cleaned_data for f in form.columns.forms ]
        partition_columns = [ f.cleaned_data for f in form.partitions.forms ]
        proposed_query = django_mako.render_to_string("create_table_statement.mako",
          {
            'database': database,
            'table': form.table.cleaned_data,
            'columns': columns,
            'partition_columns': partition_columns
          }
        )
        # Mako outputs bytestring in utf8
        proposed_query = proposed_query.decode('utf-8')
        table_name = form.table.cleaned_data['name']
        return _submit_create_and_load(request, proposed_query, table_name, None, False, database=database)
  else:
    form.bind()

  return render("create_table_manually.mako", request, {
    'action': "#",
    'table_form': form.table,
    'columns_form': form.columns,
    'partitions_form': form.partitions,
    'has_tables': len(dbms.get(request.user).get_tables()) > 0,
    'database': database,
  })


IMPORT_PEEK_SIZE = 8192
IMPORT_PEEK_NLINES = 10
DELIMITERS = [ hive_val for hive_val, desc, ascii in TERMINATORS ]
DELIMITER_READABLE = {'\\001' : _('ctrl-As'),
                      '\\002' : _('ctrl-Bs'),
                      '\\003' : _('ctrl-Cs'),
                      '\\t'   : _('tabs'),
                      ','     : _('commas'),
                      ' '     : _('spaces')}
FILE_READERS = [ ]

def import_wizard(request, database=None):
  """
  Help users define table and based on a file they want to import to Hive.
  Limitations:
    - Rows are delimited (no serde).
    - No detection for map and array types.
    - No detection for the presence of column header in the first row.
    - No partition table.
    - Does not work with binary data.
  """
  database = _get_last_database(request, database)
  encoding = i18n.get_site_encoding()
  app_name = get_app_name(request)

  if request.method == 'POST':
    # Have a while loop to allow an easy way to break
    for _ in range(1):
      #
      # General processing logic:
      # - We have 3 steps. Each requires the previous.
      #   * Step 1      : Table name and file location
      #   * Step 2a     : Display sample with auto chosen delim
      #   * Step 2b     : Display sample with user chosen delim (if user chooses one)
      #   * Step 3      : Display sample, and define columns
      # - Each step is represented by a different form. The form of an earlier step
      #   should be present when submitting to a later step.
      # - To preserve the data from the earlier steps, we send the forms back as
      #   hidden fields. This way, when users revisit a previous step, the data would
      #   be there as well.
      #
      delim_is_auto = False
      fields_list, n_cols = [ [] ], 0
      s3_col_formset = None

      # Everything requires a valid file form
      db = dbms.get(request.user)
      s1_file_form = CreateByImportFileForm(request.POST, db=db)
      if not s1_file_form.is_valid():
        break

      do_s2_auto_delim = request.POST.get('submit_file')        # Step 1 -> 2
      do_s2_user_delim = request.POST.get('submit_preview')     # Step 2 -> 2
      do_s3_column_def = request.POST.get('submit_delim')       # Step 2 -> 3
      do_hive_create = request.POST.get('submit_create')        # Step 3 -> execute

      cancel_s2_user_delim = request.POST.get('cancel_delim')   # Step 2 -> 1
      cancel_s3_column_def = request.POST.get('cancel_create')  # Step 3 -> 2

      # Exactly one of these should be True
      assert len(filter(None, (do_s2_auto_delim,
                               do_s2_user_delim,
                               do_s3_column_def,
                               do_hive_create,
                               cancel_s2_user_delim,
                               cancel_s3_column_def))) == 1, 'Invalid form submission'

      #
      # Fix up what we should do in case any form is invalid
      #
      if not do_s2_auto_delim:
        # We should have a valid delim form
        s2_delim_form = CreateByImportDelimForm(request.POST)
        if not s2_delim_form.is_valid():
          # Go back to picking delimiter
          do_s2_user_delim, do_s3_column_def, do_hive_create = True, False, False

      if do_hive_create:
        # We should have a valid columns formset
        s3_col_formset = ColumnTypeFormSet(prefix='cols', data=request.POST)
        if not s3_col_formset.is_valid():
          # Go back to define columns
          do_s3_column_def, do_hive_create = True, False

      #
      # Go to step 2: We've just picked the file. Preview it.
      #
      if do_s2_auto_delim:
        delim_is_auto = True
        fields_list, n_cols, s2_delim_form = _delim_preview(
                                              request.fs,
                                              s1_file_form,
                                              encoding,
                                              [ reader.TYPE for reader in FILE_READERS ],
                                              DELIMITERS,
                                              False)

      if (do_s2_user_delim or do_s3_column_def or cancel_s3_column_def) and s2_delim_form.is_valid():
        # Delimit based on input
        fields_list, n_cols, s2_delim_form = _delim_preview(
                                              request.fs,
                                              s1_file_form,
                                              encoding,
                                              (s2_delim_form.cleaned_data['file_type'],),
                                              (s2_delim_form.cleaned_data['delimiter'],),
                                              s2_delim_form.cleaned_data.get('read_column_headers'))

      if do_s2_auto_delim or do_s2_user_delim or cancel_s3_column_def:
        return render('choose_delimiter.mako', request, {
          'action': reverse(app_name + ':import_wizard', kwargs={'database': database}),
          'delim_readable': DELIMITER_READABLE.get(s2_delim_form['delimiter'].data[0], s2_delim_form['delimiter'].data[1]),
          'initial': delim_is_auto,
          'file_form': s1_file_form,
          'delim_form': s2_delim_form,
          'fields_list': fields_list,
          'delimiter_choices': TERMINATOR_CHOICES,
          'n_cols': n_cols,
          'database': database,
        })

      #
      # Go to step 3: Define column.
      #
      if do_s3_column_def:
        read_column_headers = s2_delim_form.cleaned_data.get('read_column_headers')
        if s3_col_formset is None or not read_column_headers:
          columns = []
          if read_column_headers and fields_list:
            first_row = fields_list[0]
            for i in range(n_cols):
              columns.append(dict(
                  column_name=first_row[i] if i < len(first_row) else 'col_%s' % (i + 1,),
                  column_type='string',
              ))
            fields_list = fields_list[1:]
          else:
            for i in range(n_cols):
              columns.append(dict(
                  column_name='col_%s' % (i + 1,),
                  column_type='string',
              ))
          s3_col_formset = ColumnTypeFormSet(prefix='cols', initial=columns)
        return render('define_columns.mako', request, {
          'action': reverse(app_name + ':import_wizard', kwargs={'database': database}),
          'file_form': s1_file_form,
          'delim_form': s2_delim_form,
          'column_formset': s3_col_formset,
          'fields_list': fields_list,
          'n_cols': n_cols,
           'database': database,
        })

      #
      # Finale: Execute
      #
      if do_hive_create:
        delim = s2_delim_form.cleaned_data['delimiter']
        table_name = s1_file_form.cleaned_data['name']
        proposed_query = django_mako.render_to_string("create_table_statement.mako",
          {
            'table': dict(name=table_name,
                          comment=s1_file_form.cleaned_data['comment'],
                          row_format='Delimited',
                          field_terminator=delim),
            'columns': [ f.cleaned_data for f in s3_col_formset.forms ],
            'partition_columns': [],
            'database': database,
          }
        )

        do_load_data = s1_file_form.cleaned_data.get('do_import')
        path = s1_file_form.cleaned_data.get('path')
        read_column_headers = s2_delim_form.cleaned_data.get('read_column_headers')
        if read_column_headers and do_load_data:
          file_type = s2_delim_form.cleaned_data.get('file_type')
          file_readers = [ reader for reader in FILE_READERS if reader.TYPE == file_type ]
          if len(file_readers) == 1:
            try:
              file_obj = request.fs.open(path)
              _skip_first_line_in_file(file_readers[0],
                                       request.fs,
                                       path,
                                       file_readers[0].find(file_obj, '\n') + 1)
            except Exception, ex:
              msg = _('Cannot process file: %s' % (ex,))
              LOG.error(msg)
              raise PopupException(msg)
        return _submit_create_and_load(request, proposed_query, table_name, path, do_load_data, database=database)
  else:
    s1_file_form = CreateByImportFileForm()

  return render('choose_file.mako', request, {
    'action': reverse(app_name + ':import_wizard', kwargs={'database': database}),
    'file_form': s1_file_form,
    'database': database,
  })


def _submit_create_and_load(request, create_hql, table_name, path, do_load, database):
  """
  Submit the table creation, and setup the load to happen (if ``do_load``).
  """
  on_success_params = {}
  app_name = get_app_name(request)

  if do_load:
    on_success_params['table'] = table_name
    on_success_params['path'] = path
    on_success_url = reverse(app_name + ':load_after_create', kwargs={'database': database})
  else:
    on_success_url = reverse(app_name + ':describe_table', kwargs={'database': database, 'table': table_name})

  query = hql_query(create_hql, database=database)
  return execute_directly(request, query,
                          on_success_url=on_success_url,
                          on_success_params=on_success_params)


def _delim_preview(fs, file_form, encoding, file_types, delimiters, read_column_headers):
  """
  _delim_preview(fs, file_form, encoding, file_types, delimiters, read_column_headers)
                              -> (fields_list, n_cols, delim_form)

  Look at the beginning of the file and parse it according to the list of
  available file_types and delimiters.
  """
  assert file_form.is_valid()

  path = file_form.cleaned_data['path']
  try:
    file_obj = fs.open(path)
    delim, file_type, fields_list = _parse_fields(
              path, file_obj, encoding, file_types, delimiters)
    file_obj.close()
  except IOError, ex:
    msg = "Failed to open file '%s': %s" % (path, ex)
    LOG.exception(msg)
    raise PopupException(msg)

  n_cols = max([ len(row) for row in fields_list ])
  # ``delimiter`` is a MultiValueField. delimiter_0 and delimiter_1 are the sub-fields.
  delimiter_0 = delim
  delimiter_1 = ''
  # If custom delimiter
  if not filter(lambda val: val[0] == delim, TERMINATOR_CHOICES):
    delimiter_0 = '__other__'
    delimiter_1 = delim

  delim_form = CreateByImportDelimForm(dict(delimiter_0=delimiter_0,
                                            delimiter_1=delimiter_1,
                                            file_type=file_type,
                                            n_cols=n_cols,
                                            read_column_headers=read_column_headers))
  if not delim_form.is_valid():
    assert False, _('Internal error when constructing the delimiter form: %(error)s') % {'error': delim_form.errors}
  return fields_list, n_cols, delim_form


def _parse_fields(path, file_obj, encoding, filetypes, delimiters):
  """
  _parse_fields(path, file_obj, encoding, filetypes, delimiters)
                                  -> (delimiter, filetype, fields_list)

  Go through the list of ``filetypes`` (gzip, text) and stop at the first one
  that works for the data. Then apply the list of ``delimiters`` and pick the
  most appropriate one.
  ``path`` is used for debugging only.

  Return the best delimiter, filetype and the data broken down into rows of fields.
  """
  file_readers = [ reader for reader in FILE_READERS if reader.TYPE in filetypes ]

  for reader in file_readers:
    LOG.debug("Trying %s for file: %s" % (reader.TYPE, path))
    file_obj.seek(0, hadoopfs.SEEK_SET)
    lines = reader.readlines(file_obj, encoding)
    if lines is not None:
      delim, fields_list = _readfields(lines, delimiters)
      return delim, reader.TYPE, fields_list
  else:
    # Even TextFileReader doesn't work
    msg = _("Failed to decode file '%(path)s' into printable characters under %(encoding)s") % {'path': path, 'encoding': encoding}
    LOG.error(msg)
    raise PopupException(msg)


def _readfields(lines, delimiters):
  """
  readfields(lines, delimiters) -> (delim, a list of lists of fields)

  ``delimiters`` is a list of escaped characters, e.g. r'\\t', r'\\001', ','

  Choose the best delimiter from the given list of delimiters. Return that delimiter
  and the fields parsed by using that delimiter.
  """
  def score_delim(fields_list):
    """
    How good is this fields_list? Score based on variance of the number of fields
    The score is always non-negative. The higher the better.
    """
    n_lines = len(fields_list)
    len_list = [ len(fields) for fields in fields_list ]

    # All lines should break into multiple fields
    if min(len_list) == 1:
      return 0

    avg_n_fields = sum(len_list) / n_lines
    sq_of_exp = avg_n_fields * avg_n_fields

    len_list_sq = [ l * l for l in len_list ]
    exp_of_sq = sum(len_list_sq) / n_lines
    var = exp_of_sq - sq_of_exp
    # Favour more fields
    return (1000.0 / (var + 1)) + avg_n_fields


  max_score = -1
  res = (None, None)

  for delim in delimiters:
    fields_list = [ ]
    for line in lines:
      if line:
        # Unescape the delimiter back to its character value
        fields_list.append(line.split(delim.decode('string_escape')))
    score = score_delim(fields_list)
    LOG.debug("'%s' gives score of %s" % (delim, score))
    if score > max_score:
      max_score = score
      res = (delim, fields_list)
  return res


def _peek_file(fs, file_form):
  """_peek_file(fs, file_form) -> (path, initial data)"""
  try:
    path = file_form.cleaned_data['path']
    file_obj = fs.open(path)
    file_head = file_obj.read(IMPORT_PEEK_SIZE)
    file_obj.close()
    return (path, file_head)
  except IOError, ex:
    msg = _("Failed to open file '%(path)s': %(error)s") % {'path': path, 'error': ex}
    LOG.exception(msg)
    raise PopupException(msg)


def _skip_first_line_in_file(reader, fs, path, path_offset = 0):
  tmp_path = path + '.tmp'
  sb = fs._stats(path)
  if sb is None:
    raise IOError(errno.ENOENT, _("Copy path '%s' does not exist") % path)
  if sb.isDir:
    raise IOError(errno.INVAL, _("Copy path '%s' is a directory") % path)
  if fs.isdir(tmp_path):
    raise IOError(errno.INVAL, _("Copy tmp_path '%s' is a directory") % tmp_path)

  offset = path_offset

  for chunk in reader.read_in_chunks(fs, path, path_offset):
    if offset == path_offset:
      fs.create(tmp_path,
                  overwrite=True,
                  blocksize=sb.blockSize,
                  replication=sb.replication,
                  permission=oct(stat.S_IMODE(sb.mode)))
    offset += len(chunk)
    reader.append(fs, tmp_path, chunk)
  fs.remove(path)
  fs.rename(tmp_path, path)

class GzipFileReader(object):
  """Class for extracting lines from a gzipped file"""
  TYPE = 'gzip'

  @staticmethod
  def readlines(fileobj, encoding):
    """readlines(fileobj, encoding) -> list of lines"""
    gz = gzip.GzipFile(fileobj=fileobj, mode='rb')
    try:
      data = gz.read(IMPORT_PEEK_SIZE)
    except IOError:
      return None
    try:
      return unicode(data, encoding, errors='replace').split('\n')[:IMPORT_PEEK_NLINES]
    except UnicodeError:
      return None

  @staticmethod
  def find(fileobj, what):
    """find(fileobj) -> first occurrence of what"""
    gz = gzip.GzipFile(fileobj=fileobj, mode='rb')
    try:
      data = gz.read(IMPORT_PEEK_SIZE)
      return data.find(what)
    except Exception:
      return -1

  @staticmethod
  def read_in_chunks(fs, path, offset):
    src_file_obj = fs.open(path, mode='r')
    gz = gzip.GzipFile(fileobj=src_file_obj, mode='rb')
    gz.read(offset)
    while True:
      chunk = gz.read(UPLOAD_CHUNK_SIZE.get())
      if chunk:
        yield chunk
      else:
        src_file_obj.close()
        return
    src_file_obj.close()

  @staticmethod
  def append(fs, path, data):
    dest_file_obj = fs.open(path, mode='w')
    gz = gzip.GzipFile(fileobj=dest_file_obj, mode='wb')
    gz.write(data)
    dest_file_obj.close()

FILE_READERS.append(GzipFileReader)


class TextFileReader(object):
  """Class for extracting lines from a regular text file"""
  TYPE = 'text'

  @staticmethod
  def readlines(fileobj, encoding):
    """readlines(fileobj, encoding) -> list of lines"""
    try:
      data = fileobj.read(IMPORT_PEEK_SIZE)
      return unicode(data, encoding, errors='replace').split('\n')[:IMPORT_PEEK_NLINES]
    except UnicodeError:
      return None

  @staticmethod
  def find(fileobj, what):
    """find(fileobj) -> first occurrence of what"""
    try:
      data = fileobj.read(IMPORT_PEEK_SIZE)
      return data.find(what)
    except Exception:
      return -1

  @staticmethod
  def read_in_chunks(fs, path, offset=0):
    while True:
      chunk = fs.read(path, offset, UPLOAD_CHUNK_SIZE.get())
      if chunk:
        offset += len(chunk)
        yield chunk
      else:
        return

  @staticmethod
  def append(fs, path, data):
    fs.append(path, data)

FILE_READERS.append(TextFileReader)


def load_after_create(request, database):
  """
  Automatically load data into a newly created table.

  We get here from the create's on_success_url, and expect to find
  ``table`` and ``path`` from the parameters.
  """
  tablename = request.REQUEST.get('table')
  path = request.REQUEST.get('path')

  if not tablename or not path:
    msg = _('Internal error: Missing needed parameter to load data into table')
    LOG.error(msg)
    raise PopupException(msg)

  LOG.debug("Auto loading data from %s into table %s" % (path, tablename))
  hql = "LOAD DATA INPATH '%s' INTO TABLE `%s.%s`" % (path, database, tablename)
  query = hql_query(hql)
  app_name = get_app_name(request)
  on_success_url = reverse(app_name + ':describe_table', kwargs={'database': database, 'table': tablename})

  return execute_directly(request, query, on_success_url=on_success_url)