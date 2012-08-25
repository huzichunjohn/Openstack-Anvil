# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import pkg_resources
import re
import weakref

from anvil import cfg
from anvil import colorizer
from anvil import component
from anvil import downloader as down
from anvil import exceptions as excp
from anvil import importer
from anvil import log as logging
from anvil import packager
from anvil import pip
from anvil import shell as sh
from anvil import trace as tr
from anvil import utils

LOG = logging.getLogger(__name__)

#### 
#### STATUS CONSTANTS
####
STATUS_INSTALLED = 'installed'
STATUS_STARTED = "started"
STATUS_STOPPED = "stopped"
STATUS_UNKNOWN = "unknown"
class ProgramStatus(object):
    def __init__(self, status, name=None, details=''):
        self.name = name
        self.status = status
        self.details = details


#### 
#### INSTALL CLASSES
####

class PkgInstallComponent(component.Component):
    def __init__(self, *args, **kargs):
        component.Component.__init__(self, *args, **kargs)
        self.tracewriter = tr.TraceWriter(self.trace_files['install'], break_if_there=False)
        self.package_registries = kargs.get('package_registries') or {}

    def _get_download_config(self):
        return None

    def _clear_package_duplicates(self, pkg_list):
        dup_free_list = []
        names_there = set()
        for pkg in pkg_list:
            if pkg['name'] not in names_there:
                dup_free_list.append(pkg)
                names_there.add(pkg['name'])
        return dup_free_list

    def _get_download_location(self):
        key = self._get_download_config()
        if not key:
            return (None, None)
        uri = self.get_option(key, '').strip()
        if not uri:
            raise ValueError(("Could not find uri in config to download "
                              "from option %s") % (key))
        return (uri, self.get_option('app_dir'))

    def download(self):
        (from_uri, target_dir) = self._get_download_location()
        if not from_uri and not target_dir:
            return []
        else:
            uris = [from_uri]
            utils.log_iterable(uris, logger=LOG,
                    header="Downloading from %s uris" % (len(uris)))
            self.tracewriter.download_happened(target_dir, from_uri)
            dirs_made = down.download(self.distro, from_uri, target_dir)
            self.tracewriter.dirs_made(*dirs_made)
            return uris

    def config_params(self, config_fn):
        mp = dict(self.params)
        if config_fn:
            mp['CONFIG_FN'] = config_fn
        return mp

    @property
    def packages(self):
        pkg_list = self.get_option('packages', [])
        if not pkg_list:
            pkg_list = []
        for name, values in self.subsystems.items():
            if 'packages' in values:
                LOG.debug("Extending package list with packages for subsystem: %r", name)
                pkg_list.extend(values.get('packages'))
        pkg_list = self._clear_package_duplicates(pkg_list)
        return pkg_list

    def _make_packager(self, name, pkg_info, default_cls):
        if name not in self.package_registries:
            self.package_registries[name] = packager.Registry()
        cls = packager.get_packager_class(pkg_info, default_cls)
        return cls(self.distro, self.package_registries[name])

    def install(self):
        LOG.debug('Preparing to install packages for: %r', self.name)
        pkgs = self.packages
        if pkgs:
            pkg_names = [p['name'] for p in pkgs]
            utils.log_iterable(pkg_names, logger=LOG,
                header="Setting up %s distribution packages" % (len(pkg_names)))
            with utils.progress_bar('Installing', len(pkgs)) as p_bar:
                for (i, p) in enumerate(pkgs):
                    installer = self._make_packager('distro', p, self.distro.package_manager_class)
                    self.tracewriter.package_installed(p)
                    installer.install(p)
                    p_bar.update(i + 1)

    def pre_install(self):
        pkgs = self.packages
        for p in pkgs:
            installer = self._make_packager('distro', p, self.distro.package_manager_class)
            installer.pre_install(p, self.params)

    def post_install(self):
        pkgs = self.packages
        for p in pkgs:
            installer = self._make_packager('distro', p, self.distro.package_manager_class)
            installer.post_install(p, self.params)

    @property
    def config_files(self):
        return []

    def _config_adjust(self, contents, config_fn):
        return contents

    def target_config(self, config_fn):
        return sh.joinpths(self.get_option('cfg_dir'), config_fn)

    def source_config(self, config_fn):
        return utils.load_template(self.name, config_fn)

    @property
    def link_dir(self):
        link_dir_base = self.distro.get_command_config('base_link_dir')
        return sh.joinpths(link_dir_base, self.name)

    @property
    def symlinks(self):
        links = {}
        for fn in self.config_files:
            source_fn = self.target_config(fn)
            links[source_fn] = sh.joinpths(self.link_dir, fn)
        return links

    def _config_param_replace(self, config_fn, contents, parameters):
        return utils.param_replace(contents, parameters)

    def _configure_files(self):
        config_fns = self.config_files
        if config_fns:
            utils.log_iterable(config_fns, logger=LOG,
                header="Configuring %s files" % (len(config_fns)))
            for fn in config_fns:
                tgt_fn = self.target_config(fn)
                self.tracewriter.dirs_made(*sh.mkdirslist(sh.dirname(tgt_fn)))
                (source_fn, contents) = self.source_config(fn)
                LOG.debug("Configuring file %s ---> %s.", (source_fn), (tgt_fn))
                contents = self._config_param_replace(fn, contents, self.config_params(fn))
                contents = self._config_adjust(contents, fn)
                self.tracewriter.cfg_file_written(sh.write_file(tgt_fn, contents))
        return len(config_fns)

    def _configure_symlinks(self):
        links = self.symlinks
        if not links:
            return 0
        # This sort happens so that we link in the correct order
        # although it might not matter. Either way. We ensure that the right
        # order happens. Ie /etc/blah link runs before /etc/blah/blah
        link_srcs = sorted(links.keys())
        link_srcs.reverse()
        links_made = 0
        link_nice = []
        for source in link_srcs:
            link = links[source]
            link_nice.append("%s => %s" % (link, source))
        utils.log_iterable(link_nice, logger=LOG,
                           header="Creating %s sym-links" % (len(link_nice)))
        for source in link_srcs:
            link = links[source]
            try:
                LOG.debug("Symlinking %s to %s.", (link), (source))
                self.tracewriter.dirs_made(*sh.symlink(source, link))
                self.tracewriter.symlink_made(link)
                links_made += 1
            except OSError as e:
                LOG.warn("Symlinking %s to %s failed: %s", colorizer.quote(link), colorizer.quote(source), e)
        return links_made

    def configure(self):
        return self._configure_files() + self._configure_symlinks()


class PythonInstallComponent(PkgInstallComponent):
    def __init__(self, *args, **kargs):
        PkgInstallComponent.__init__(self, *args, **kargs)
        self.requires_files = [
            sh.joinpths(self.get_option('app_dir'), 'tools', 'pip-requires'),
        ]
        if self.get_bool_option('use_tests_requires', True):
            self.requires_files.append(sh.joinpths(self.get_option('app_dir'), 'tools', 'test-requires'))

    def _get_download_config(self):
        return 'get_from'

    @property
    def python_directories(self):
        py_dirs = {}
        app_dir = self.get_option('app_dir')
        if sh.isdir(app_dir):
            py_dirs[self.name] = app_dir
        return py_dirs

    @property
    def packages(self):
        pkg_list = super(PythonInstallComponent, self).packages
        if not pkg_list:
            pkg_list = []
        pkg_list.extend(self._get_mapped_packages())
        return pkg_list

    @property
    def pips_to_packages(self):
        pip_pkg_list = self.get_option('pip_to_package', [])
        if not pip_pkg_list:
            pip_pkg_list = []
        return pip_pkg_list

    def _match_pip_requires(self, pip_name):

        # TODO(harlowja) Is this a bug?? that this is needed?
        def pip_match(in1, in2):
            in1 = in1.replace("-", "_")
            in1 = in1.lower()
            in2 = in2.replace('-', '_')
            in2 = in2.lower()
            return in1 == in2

        pip_found = False
        pkg_found = None

        # Try to find it in anyones pip -> pkg list
        pip2_pkg_mp = {
            self.name: self.pips_to_packages,
        }
        for (name, c) in self.instances.items():
            if c is self or not c.activated:
                continue
            if isinstance(c, (PythonInstallComponent)):
                pip2_pkg_mp[name] = c.pips_to_packages
        for (who, pips_2_pkgs) in pip2_pkg_mp.items():
            for pip_info in pips_2_pkgs:
                if pip_match(pip_name, pip_info['name']):
                    pip_found = True
                    pkg_found = pip_info.get('package')
                    LOG.debug("Matched pip->pkg (%s) from component %s", pip_name, who)
                    break
            if pip_found:
                break
        if pip_found:
            return (pkg_found, False)

        # Ok nobody had it in a pip->pkg mapping
        # but see if they had it in there pip collection
        pip_mp = {
            self.name: list(self.pips),
        }
        for (name, c) in self.instances.items():
            if not c.activated or c is self:
                continue
            if isinstance(c, (PythonInstallComponent)):
                pip_mp[name] = list(c.pips)
        pip_found = False
        pip_who = None
        for (who, pips) in pip_mp.items():
            for pip_info in pips:
                if pip_match(pip_info['name'], pip_name):
                    pip_found = True
                    pip_who = pip_info
                    LOG.debug("Matched pip (%s) from component %s", pip_name, who)
                    break
            if pip_found:
                break
        if pip_found:
            return (pip_who, True)

        return (None, False)

    def _get_mapped_packages(self):
        add_on_pkgs = []
        all_pips = []
        for fn in self.requires_files:
            all_pips.extend(self._extract_pip_requires(fn))
        for (_requirement, (pkg_info, from_pip)) in all_pips:
            if from_pip or not pkg_info:
                continue
            add_on_pkgs.append(pkg_info)
        return add_on_pkgs

    @property
    def pips(self):
        pip_list = self.get_option('pips')
        if not pip_list:
            pip_list = []
        for (name, values) in self.subsystems.items():
            if 'pips' in values:
                LOG.debug("Extending pip list with pips for subsystem: %r" % (name))
                pip_list.extend(values.get('pips'))
        pip_list = self._clear_package_duplicates(pip_list)
        return pip_list

    def _install_pips(self):
        pips = self.pips
        if pips:
            pip_names = [p['name'] for p in pips]
            utils.log_iterable(pip_names, logger=LOG,
                header="Setting up %s python packages" % (len(pip_names)))
            with utils.progress_bar('Installing', len(pips)) as p_bar:
                for (i, p) in enumerate(pips):
                    self.tracewriter.pip_installed(p)
                    installer = self._make_packager('pip', p, pip.Packager)
                    installer.install(p)
                    p_bar.update(i + 1)

    def _clean_pip_requires(self):
        # Fixup these files if they exist (sometimes they have 'junk' in them)
        req_fns = []
        for fn in self.requires_files:
            if not sh.isfile(fn):
                continue
            req_fns.append(fn)
        if req_fns:
            utils.log_iterable(req_fns, logger=LOG,
                header="Adjusting %s pip 'requires' files" % (len(req_fns)))
            for fn in req_fns:
                new_lines = []
                for line in sh.load_file(fn).splitlines():
                    s_line = line.strip()
                    if len(s_line) == 0:
                        continue
                    elif s_line.startswith("#"):
                        new_lines.append(s_line)
                    elif not self._filter_pip_requires_line(s_line):
                        new_lines.append(("# %s" % (s_line)))
                    else:
                        new_lines.append(s_line)
                sh.move(fn, "%s.orig" % (fn))
                new_fc = "\n".join(new_lines)
                sh.write_file(fn, "# Cleaned on %s\n\n%s\n" % (utils.rcf8222date(), new_fc))
        return len(req_fns)

    def _filter_pip_requires_line(self, line):
        return line

    def pre_install(self):
        PkgInstallComponent.pre_install(self)
        for p in self.pips:
            installer = self._make_packager('pip', p, pip.Packager)
            installer.pre_install(p, self.params)

    def post_install(self):
        PkgInstallComponent.post_install(self)
        for p in self.pips:
            installer = self._make_packager('pip', p, pip.Packager)
            installer.post_install(p, self.params)

    def _install_python_setups(self):
        py_dirs = self.python_directories
        if py_dirs:
            real_dirs = dict()
            for (name, wkdir) in py_dirs.items():
                real_dirs[name] = wkdir
                if not real_dirs[name]:
                    real_dirs[name] = self.get_option('app_dir')
            utils.log_iterable(real_dirs.values(), logger=LOG,
                header="Setting up %s python directories" % (len(real_dirs)))
            setup_cmd = self.distro.get_command('python', 'setup')
            for (name, working_dir) in real_dirs.items():
                self.tracewriter.dirs_made(*sh.mkdirslist(working_dir))
                self.tracewriter.py_installed(name, working_dir)
                root_fn = sh.joinpths(self.get_option('trace_dir'),
                                      "%s.python.setup" % (name))
                sh.execute(*setup_cmd,
                           cwd=working_dir,
                           run_as_root=True,
                           stderr_fn='%s.stderr' % (root_fn),
                           stdout_fn='%s.stdout' % (root_fn),
                           trace_writer=self.tracewriter
                           )

    def _python_install(self):
        self._install_pips()
        self._install_python_setups()

    def _extract_pip_requires(self, fn):
        if not sh.isfile(fn):
            return []
        LOG.debug("Resolving dependencies from %s.", colorizer.quote(fn))
        pips_needed = []
        for line in sh.load_file(fn).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pips_needed.append(pkg_resources.Requirement.parse(line))
        if not pips_needed:
            return
        matchings = []
        for requirement in pips_needed:
            matchings.append([requirement, self._match_pip_requires(requirement.project_name)])
        return matchings

    def _verify_pip_requires(self):
        all_pips = []
        for fn in self.requires_files:
            all_pips.extend(self._extract_pip_requires(fn))
        for (requirement, (pkg_info, _from_pip)) in all_pips:
            if not pkg_info:
                raise excp.DependencyException(("Pip dependency %r is not translatable to a listed"
                                                " (from this or previously activated components) pip package"
                                                ' or a pip->package mapping!') % (requirement))

    def install(self):
        # TODO(harlowja) can we move this to the preinstall phase instead of just before install??
        self._verify_pip_requires()
        PkgInstallComponent.install(self)
        self._python_install()

    def configure(self):
        configured_am = PkgInstallComponent.configure(self)
        configured_am += self._clean_pip_requires()
        return configured_am


#### 
#### RUNTIME CLASSES
####

class ProgramRuntime(component.Component):
    @property
    def apps_to_start(self):
        return []

    def app_options(self, app_name):
        return []

    def app_params(self, app_name):
        mp = dict(self.params)
        if app_name:
            mp['APP_NAME'] = app_name
        return mp

    def restart(self):
        return 0

    def post_start(self):
        pass

    def pre_start(self):
        pass

    def status(self):
        return []

    def start(self):
        return 0


class EmptyRuntime(ProgramRuntime):
    pass


class PythonRuntime(ProgramRuntime):
    def __init__(self, *args, **kargs):
        ProgramRuntime.__init__(self, *args, **kargs)
        self.tracewriter = tr.TraceWriter(self.trace_files['start'], break_if_there=True)
        self.tracereader = tr.TraceReader(self.trace_files['start'])

    def start(self):
        # Anything to start?
        am_started = 0
        # Select how we are going to start it
        run_type = self.get_option("run_type", 'anvil.runners.fork:ForkRunner')
        starter_cls = importer.import_entry_point(run_type)
        starter = starter_cls(self)
        for i, app_info in enumerate(self.apps_to_start):
            self._start_app(app_info, run_type, starter)
            am_started = i + 1
            self._post_app_start(app_info)
        return am_started

    def _start_app(self, app_info, run_type, starter):
        app_name = app_info["name"]
        app_pth = app_info.get("path", app_name)
        app_dir = app_info.get("app_dir", self.get_option('app_dir'))
        program_opts = utils.param_replace_list(self.app_options(app_name), self.app_params(app_name))
        LOG.debug("Starting %r using %r", app_name, starter)
        details_fn = starter.start(app_name, app_pth=app_pth, app_dir=app_dir, opts=program_opts)
        LOG.info("Started sub-program %s.", colorizer.quote(app_name))
        # This trace is used to locate details about what/how to stop
        self.tracewriter.app_started(app_name, details_fn, run_type)

    def _post_app_start(self, app_info):
        if 'sleep_time' in app_info:
            LOG.info("%s requested a %s second sleep time, please wait...", 
                     colorizer.quote(app_info.get('name')), app_info.get('sleep_time'))
            sh.sleep(float(app_info.get('sleep_time')))

    def _locate_investigators(self, apps_started):
        investigator_created = {}
        to_investigate = []
        for (app_name, _trace_fn, run_type) in apps_started:
            inv_cls = None
            try:
                inv_cls = importer.import_entry_point(run_type)
            except RuntimeError as e:
                LOG.warn("Could not load class %s which should be used to investigate %s: %s",
                         colorizer.quote(run_type), colorizer.quote(app_name), e)
                continue
            investigator = None
            if inv_cls in investigator_created:
                investigator = investigator_created[inv_cls]
            else:
                investigator = inv_cls(self)
                investigator_created[inv_cls] = investigator
            to_investigate.append((app_name, investigator))
        return to_investigate

    def stop(self):
        # Anything to stop??
        killed_am = 0
        apps_started = 0
        try:
            apps_started = self.tracereader.apps_started()
        except excp.NoTraceException:
            pass
        if not apps_started:
            return killed_am
        to_kill = self._locate_investigators(apps_started)
        for (app_name, handler) in to_kill:
            handler.stop(app_name)
            killed_am += 1
        if len(apps_started) == killed_am:
            sh.unlink(self.tracereader.filename())
        return killed_am

    def status(self):
        statii = []
        apps_started = None
        try:
            apps_started = self.tracereader.apps_started()
        except excp.NoTraceException:
            pass
        if not apps_started:
            return statii
        to_check = self._locate_investigators(apps_started)
        for (name, handler) in to_check:
            (status, details) = handler.status(name)
            statii.append(ProgramStatus(name=name,
                                        status=status,
                                        details=details))
        return statii


#### 
#### UNINSTALL CLASSES
####

class PkgUninstallComponent(component.Component):
    def __init__(self, *args, **kargs):
        component.Component.__init__(self, *args, **kargs)
        self.tracereader = tr.TraceReader(self.trace_files['install'])
        self.package_registries = kargs.get('package_registries', {})

    def _make_packager(self, name, pkg_info, default_cls):
        if name not in self.package_registries:
            self.package_registries[name] = packager.Registry()
        cls = packager.get_packager_class(pkg_info, default_cls)
        return cls(self.distro, self.package_registries[name])

    def unconfigure(self):
        self._unconfigure_files()
        self._unconfigure_links()

    def _unconfigure_links(self):
        sym_files = self.tracereader.symlinks_made()
        if sym_files:
            utils.log_iterable(sym_files, logger=LOG,
                header="Removing %s symlink files" % (len(sym_files)))
            for fn in sym_files:
                sh.unlink(fn, run_as_root=True)

    def _unconfigure_files(self):
        cfg_files = self.tracereader.files_configured()
        if cfg_files:
            utils.log_iterable(cfg_files, logger=LOG,
                header="Removing %s configuration files" % (len(cfg_files)))
            for fn in cfg_files:
                sh.unlink(fn, run_as_root=True)

    def uninstall(self):
        self._uninstall_pkgs()
        self._uninstall_touched_files()
        self._uninstall_dirs()
        LOG.debug("Deleting install trace file %r", self.tracereader.filename())
        sh.unlink(self.tracereader.filename())

    def post_uninstall(self):
        pass

    def pre_uninstall(self):
        pass

    def _uninstall_pkgs(self):
        pkgs = self.tracereader.packages_installed()
        if pkgs:
            pkg_names = set([p['name'] for p in pkgs])
            utils.log_iterable(pkg_names, logger=LOG,
                header="Potentially removing %s packages" % (len(pkg_names)))
            which_removed = set()
            with utils.progress_bar('Uninstalling', len(pkgs), reverse=True) as p_bar:
                for (i, p) in enumerate(pkgs):
                    uninstaller = self._make_packager('distro', p, self.distro.package_manager_class)
                    if uninstaller.remove(p):
                        which_removed.add(p['name'])
                    p_bar.update(i + 1)
            utils.log_iterable(which_removed, logger=LOG,
                    header="Actually removed %s packages" % (len(which_removed)))

    def _uninstall_touched_files(self):
        files_touched = self.tracereader.files_touched()
        if files_touched:
            utils.log_iterable(files_touched, logger=LOG,
                header="Removing %s touched files" % (len(files_touched)))
            for fn in files_touched:
                sh.unlink(fn, run_as_root=True)

    def _uninstall_dirs(self):
        dirs_made = self.tracereader.dirs_made()
        if dirs_made:
            dirs_made = [sh.abspth(d) for d in dirs_made]
            download_places = [path_location[0] for path_location in self.tracereader.download_locations()]
            if download_places:
                utils.log_iterable(download_places, logger=LOG,
                    header="Keeping %s download directories (and there children directories)" % (len(download_places)))
                for download_place in download_places:
                    dirs_made = sh.remove_parents(download_place, dirs_made)
            if dirs_made:
                utils.log_iterable(dirs_made, logger=LOG,
                    header="Removing %s created directories" % (len(dirs_made)))
                for dir_name in dirs_made:
                    if sh.isdir(dir_name):
                        sh.deldir(dir_name, run_as_root=True)
                    else:
                        LOG.warn("No directory found at %s - skipping", colorizer.quote(dir_name, quote_color='red'))


class PythonUninstallComponent(PkgUninstallComponent):

    def uninstall(self):
        self._uninstall_python()
        self._uninstall_pips()
        PkgUninstallComponent.uninstall(self)

    def _uninstall_pips(self):
        pips = self.tracereader.pips_installed()
        if pips:
            pip_names = set([p['name'] for p in pips])
            utils.log_iterable(pip_names, logger=LOG,
                header="Uninstalling %s python packages" % (len(pip_names)))
            with utils.progress_bar('Uninstalling', len(pips), reverse=True) as p_bar:
                for (i, p) in enumerate(pips):
                    try:
                        uninstaller = self._make_packager('pip', p, pip.Packager)
                        uninstaller.remove(p)
                    except excp.ProcessExecutionError as e:
                        # NOTE(harlowja): pip seems to die if a pkg isn't there even in quiet mode
                        combined = (str(e.stderr) + str(e.stdout))
                        if not re.search(r"not\s+installed", combined, re.I):
                            raise
                    p_bar.update(i + 1)

    def _uninstall_python(self):
        py_listing = self.tracereader.py_listing()
        if py_listing:
            py_listing_dirs = set()
            for (_name, where) in py_listing:
                py_listing_dirs.add(where)
            utils.log_iterable(py_listing_dirs, logger=LOG,
                header="Uninstalling %s python setups" % (len(py_listing_dirs)))
            unsetup_cmd = self.distro.get_command('python', 'unsetup')
            for where in py_listing_dirs:
                if sh.isdir(where):
                    sh.execute(*unsetup_cmd, cwd=where, run_as_root=True)
                else:
                    LOG.warn("No python directory found at %s - skipping", colorizer.quote(where, quote_color='red'))


#### 
#### TESTING CLASSES
####


class EmptyTestingComponent(component.Component):
    def run_tests(self):
        return


class PythonTestingComponent(component.Component):
    def _get_test_exclusions(self):
        return []
    
    def _use_run_tests(self):
        return True
    
    def _get_test_command(self):
        # See: http://docs.openstack.org/developer/nova/devref/unit_tests.html
        # And: http://wiki.openstack.org/ProjectTestingInterface
        app_dir = self.get_option('app_dir')
        if sh.isfile(sh.joinpths(app_dir, 'run_tests.sh')) and self._use_run_tests():
            cmd = [sh.joinpths(app_dir, 'run_tests.sh'), '-N', '-P']
        else:
            # Assume tox is being used, which we can't use directly
            # since anvil doesn't really do venv stuff (its meant to avoid those...)
            cmd = ['nosetests']
        # See: $ man nosetests
        cmd.append('--nologcapture')
        for e in self._get_test_exclusions():
            cmd.append('--exclude=%s' % (e))
        return cmd

    def _get_env(self):
        env_addons = {}
        app_dir = self.get_option('app_dir')
        tox_fn = sh.joinpths(app_dir, 'tox.ini')
        if sh.isfile(tox_fn):
            try:
                tox_cfg = cfg.BuiltinConfigParser(fns=[tox_fn])
                env_values = tox_cfg.get('testenv', 'setenv') or ''
                for env_line in env_values.splitlines():
                    env_line = env_line.strip()
                    env_line = env_line.split("#")[0].strip()
                    if not env_line:
                        continue
                    env_entry = env_line.split('=', 1)
                    if len(env_entry) == 2:
                        (name, value) = env_entry
                        name = name.strip()
                        value = value.strip()
                        if name.lower() != 'virtual_env':
                            env_addons[name] = value
            except IOError:
                pass
        return env_addons

    def run_tests(self):
        app_dir = self.get_option('app_dir')
        if not sh.isdir(app_dir):
            LOG.warn("Unable to find application directory at %s, can not run %s tests.", app_dir, self.name)
            return
        cmd = self._get_test_command()
        env = self._get_env()
        sh.execute(*cmd, stdout_fh=None, stderr_fh=None, cwd=app_dir, env_overrides=env)


#### 
#### PACKAGING CLASSES
####

class EmptyPackagingComponent(component.Component):
    def package(self):
        return None
