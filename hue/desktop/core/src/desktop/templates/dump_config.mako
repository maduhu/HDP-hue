## Licensed to Cloudera, Inc. under one
## or more contributor license agreements.  See the NOTICE file
## distributed with this work for additional information
## regarding copyright ownership.  Cloudera, Inc. licenses this file
## to you under the Apache License, Version 2.0 (the
## "License"); you may not use this file except in compliance
## with the License.  You may obtain a copy of the License at
##
##     http://www.apache.org/licenses/LICENSE-2.0
##
## Unless required by applicable law or agreed to in writing, software
## distributed under the License is distributed on an "AS IS" BASIS,
## WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
## See the License for the specific language governing permissions and
## limitations under the License.

## Note that this is similar to the config_dump management command.
<%!
from desktop.lib.conf import BoundContainer, is_anonymous
from desktop.views import commonheader, commonfooter
from django.utils.translation import ugettext as _
%>

<%namespace name="layout" file="about_layout.mako" />

${ commonheader(_('About'), "about", user, "100px") | n,unicode }
${layout.menubar(section='dump_config')}

    <div class="container-fluid">

        ${_('Configuration files located in')} <code>${conf_dir}</code>
        <br/><br/>

        <h2>${_('Installed applications')}</h2>
        <ul>
        % for app in apps:
            <li>${app.name}</li>
        % endfor
        </ul>

        <h2>${_('Configuration Sections and Variables')}</h2>

        <ul class="nav nav-tabs">
            % for obj in top_level:
                <li
                    % if loop.first:
                        class="active"
                    % endif
                ><a href="#${obj.config.key}Conf" data-toggle="tab">${obj.config.key}</a></li>
            % endfor
        </ul>

        <%def name="showTopLevel(config_obj, depth=0)">
            <div class="tab-content">
                % for v in config_obj:
                    <%
                        # Don't recurse into private variables.
                        if v.config.private and not show_private:
                            continue
                    %>
                    <div id="${v.config.key}Conf" class="tab-pane
                    % if loop.first:
                        active
                    % endif
                    ">
                        ${recurse(v, depth + 1)}
                  </div>
                % endfor
            </div>
        </%def>

        <%def name="recurse(config_obj, depth=0)">
            <table class="table table-striped">
            <tr>
             % if depth > 1:
              <th>
              % if is_anonymous(config_obj.config.key):
                <i>(default section)</i>
              % else:
                ${config_obj.config.key}
              % endif
              </th>
             % endif
             % if depth == 1:
                <td style="border-top:0">
             % else:
                  <td>
             % endif
              <% hasObjects=False%>
              % if isinstance(config_obj, BoundContainer):
                % for v in config_obj.get().values():
            <%
                  # Don't recurse into private variables.
                  if v.config.private and not show_private:
                    continue
            %>
                % if not hasObjects and config_obj.config.help:
                    <p class="dump_config_help"><i>${config_obj.config.help}</i></p>
                    <% hasObjects=True %>
                % endif
                ${recurse(v, depth + 1)}
                % endfor
                % if not hasObjects:
                    <p class="dump_config_help"><i>${_('No variables are currently configured for the ')}${config_obj.config.key}${_(' application')}</i></p>
                % endif
              % else:
                <p>${str(config_obj.get_raw()).decode('utf-8', 'replace')}</p>
                % if config_obj.config.help:
                <p class="dump_config_help"><i>${config_obj.config.help}</i></p>
                % endif
                <p class="dump_config_default">${_('Default:')} <i>${str(config_obj.config.default).decode('utf-8', 'replace')}</i></p>
              % endif
              </td>
            </tr>
            </table>
        </%def>

        ${showTopLevel(top_level)}

        <br/>
        <br/>
        <br/>

    </div>

${ commonfooter(messages) | n,unicode }