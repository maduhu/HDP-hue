## Licensed to the Apache Software Foundation (ASF) under one
## or more contributor license agreements.  See the NOTICE file
## distributed with this work for additional information
## regarding copyright ownership.  The ASF licenses this file
## to you under the Apache License, Version 2.0 (the
## "License"); you may not use this file except in compliance
## with the License.  You may obtain a copy of the License at
##
## http://www.apache.org/licenses/LICENSE-2.0
##
## Unless required by applicable law or agreed to in writing,
## software distributed under the License is distributed on an
## "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
## KIND, either express or implied.  See the License for the
## specific language governing permissions and limitations
## under the License.

<%!
    from desktop.views import commonheader, commonfooter
    from django.utils.translation import ugettext as _
%>

<%namespace name="comps" file="beeswax_components.mako" />

<table class="table table-condensed table-striped datatables">
    <thead>
    <tr>
        <th width="1%"><div class="hueCheckbox selectAll" data-selectables="databaseCheck"></div></th>
        <th>${_('Database Name')}</th>
        <th>&nbsp;</th>
    </tr>
    </thead>
    <tbody>
            % for database in databases:
            <tr>
                <td data-row-selector-exclude="true" width="1%">
                    <div class="hueCheckbox databaseCheck"
                         data-drop-name="${ database }" data-row-selector-exclude="true"></div>
                </td>
                <td>
                    <a href="${ url(app_name + ':show_tables') }" data-row-selector="true">${ database }</a>
                </td>
            </tr>
            % endfor
    </tbody>
</table>