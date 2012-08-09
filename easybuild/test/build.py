##
# Copyright 2012 Toon Willems
#
# This file is part of EasyBuild,
# originally created by the HPC team of the University of Ghent (http://ugent.be/hpc).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
regtest script which will build (in parallel) the given easyconfigs.
reports errors afterwards (in log and in junit-compatible xml file)

There are several possibilities why some applications fail to build,
this test will distinguish between the different phases in the build:
   * eb-file parsing
   * initialization
   * preparation
   * pre-build verification
   * generate installdir name
   * make builddir
   * unpacking
   * patching
   * prepare toolkit
   * setup startfrom
   * configure
   * make
   * test
   * create installdir
   * make install
   * packages
   * postproc
   * sanity check
   * cleanup

"""
import copy
import glob
import logging
import platform
import os
import re
import sys
import time
import xml.dom.minidom as xml
from datetime import datetime
from optparse import OptionParser

import easybuild
import easybuild.tools.config as config
import easybuild.tools.systemtools as systemtools
import easybuild.tools.build_log as build_log
from easybuild.tools.build_log import getLog, EasyBuildError, initLogger
from easybuild.framework.application import get_class, Application
from easybuild.build import findEasyconfigs, processEasyconfig, resolveDependencies
from easybuild.tools.filetools import modifyEnv
from easybuild.tools.pbs_job import PbsJob

# some variables used by different functions
initLogger(filename=None, debug=True, typ=None)
log = getLog("ParallelBuild")
test_results = []
build_stopped = {}


def main():
    """ main entry point """
    # assume default config path
    config.init('easybuild/easybuild_config.py')
    cur_dir = os.getcwd()

    # Option parsing
    parser = OptionParser()
    parser.add_option("--no-parallel", action="store_false", dest="parallel", default=True,
            help="specify this option if you want to prevent parallel build")
    parser.add_option("--output-dir", dest="directory", help="set output directory for test-run")
    parser.add_option("-r", "--robot", default="easybuild/easyconfigs",
            help="specify robot directory (default: %default)")
    parser.add_option("-a", "--aggregate", help="collect all the xmls inside the given directory and generate a single file")

    (opts, args) = parser.parse_args()

    if opts.aggregate:
        output_file = os.path.join(opts.aggregate, "easybuild-aggregate.xml")
        aggregate_xml_in_dirs(opts.aggregate, output_file)
        log.info("aggregated xml files inside %s, output written to: %s" % (opts.aggregate, output_file))
        sys.exit(0)

    # Create base directory inside the current directory. This will be used to place
    # all log files and the test output as xml
    basename = "easybuild-test-%s" % datetime.now().strftime("%d-%m-%Y-%H:%M:%S")
    if opts.directory:
        output_dir = opts.directory
    elif "EASYBUILDTESTOUTPUT" in os.environ:
        output_dir = os.path.abspath(os.environ['EASYBUILDTESTOUTPUT'])
    else:
        # Use default: Current dir + easybuil-test-timestamp
        output_dir = os.path.join(cur_dir, basename)

    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # find all easyconfigs (either in specified paths or in 'easybuild/easyblocks')
    files = []
    if args:
        for path in args:
            files += findEasyconfigs(path, log)
    else:
        # Default path
        path = "easybuild/easyconfigs/"
        files = findEasyconfigs(path, log)

    # process all the found easyconfig files
    packages = []
    for file in files:
        try:
            packages.extend(processEasyconfig(file, log, None))
        except EasyBuildError, err:
            test_results.append((file, 'eb-file error', err))

    if opts.parallel:
        resolved = resolveDependencies(packages, opts.robot, log)
        build_packages_in_parallel(resolved, output_dir, cur_dir)
    else:
        build_packages(packages, output_dir)


def perform_step(fase, obj, method):
    """
    Perform method on object if it can be build
    """
    if obj not in build_stopped:
        try:
            method(obj)
        except EasyBuildError, err:
            # we cannot continue building it
            test_results.append((obj, fase, err))
            # keep a dict of so we can check in O(1) if objects can still be build
            build_stopped[obj] = fase


def get_instance(package):
    """ get an instance for this package """
    spec = package['spec']
    name = package['module'][0]

    # handle easyconfigs with custom easyblocks
    easyblock = None
    reg = re.compile(r"^\s*easyblock\s*=(.*)$")
    for line in open(spec).readlines():
        match = reg.search(line)
        if match:
            easyblock = eval(match.group(1))
            break

    app_class = get_class(easyblock, log, name=name)
    return app_class(spec, debug=True)


def build_packages(packages, output_dir):
    """
    build the packages
    """
    apps = []
    for pkg in packages:
        try:
            instance = get_instance(pkg)
            apps.append(instance)
        except EasyBuildError, err:
            test_results.append((pkg['spec'], 'initialization', err))


    base_dir = os.getcwd()
    base_env = copy.deepcopy(os.environ)
    succes = []

    for app in apps:
        start_time = time.time()
        # start with a clean slate
        os.chdir(base_dir)
        modifyEnv(os.environ, base_env)

        # create a handler per app so we can capture debug output per application
        handler = logging.FileHandler(os.path.join(output_dir, "%s-%s.log" % (app.name(), app.installversion())))
        handler.setFormatter(build_log.formatter)

        app.log.addHandler(handler)

        # take manual control over the building
        perform_step("preparation", app, lambda x: x.prepare_build())
        perform_step("pre-build verification", app, lambda x: x.ready2build())
        perform_step("generate installdir name", app, lambda x: x.gen_installdir())
        perform_step("make builddir", app, lambda x: x.make_builddir())
        perform_step("unpacking", app, lambda x: x.unpack_src())
        perform_step("patching", app, lambda x: x.apply_patch())
        perform_step("prepare toolkit", app, lambda x: x.toolkit().prepare(x.getcfg('onlytkmod')))
        perform_step("setup startfrom", app, lambda x: x.startfrom())
        perform_step('configure', app, lambda x: x.configure())
        perform_step('make', app, lambda x: x.make())
        perform_step('test', app, lambda x: x.test())
        perform_step('create installdir', app, lambda x: x.make_installdir())
        perform_step('make install', app, lambda x: x.make_install())
        perform_step('packages', app, lambda x: x.packages())
        perform_step('postproc', app, lambda x: x.postproc())
        perform_step('sanity check', app, lambda x: x.sanitycheck())
        perform_step('cleanup', app, lambda x: x.cleanup())
        perform_step('make module', app, lambda x: x.make_module())

        # remove handler
        app.log.removeHandler(handler)

        if app not in build_stopped:
            # gather build stats
            build_time = round(time.time() - start_time, 2)

            buildstats = {
                          'build_time': build_time,
                          'platform': platform.platform(),
                          'core_count': systemtools.get_core_count(),
                          'cpu_model': systemtools.get_cpu_model(),
                          'install_size': app.installsize(),
                          'timestamp': int(time.time()),
                          'host': os.uname()[1],
                         }
            succes.append((app, buildstats))

    for result in test_results:
        log.info("%s crashed with an error during fase: %s, error: %s" % result)

    failed = len(build_stopped)
    total = len(apps)

    log.info("%s from %s packages failed to build!" % (failed, total))

    output_file = os.path.join(output_dir, "easybuild-test.xml")
    log.debug("writing xml output to %s" % output_file)
    write_to_xml(succes, test_results, output_file)


def build_packages_in_parallel(packages, output_dir, script_dir):
    """
    list is a list of packages which can be build! (e.g. they have no unresolved dependencies)
    this function will build them in parallel by submitting jobs
    """
    # first find those without dependencies
    no_dependencies = [pkg for pkg in packages if len(pkg['unresolvedDependencies']) == 0]
    with_dependencies = [pkg for pkg in packages if pkg not in no_dependencies]

    # This is very important, otherwise we might have race conditions
    # e.g. GCC-4.5.3 finds cloog.tar.gz but it was incorrectly downloaded by GCC-4.6.3
    # running this step here, prevents this
    log.info("preparing packages: %s" % no_dependencies)
    for pkg in no_dependencies:
        prepare_package(pkg)

    # we submit all the jobs which can be trivially build.
    jobs = [create_job(pkg, output_dir, script_dir) for pkg in no_dependencies]
    job_module_dict = {}

    # submit in different loop (otherwise we'd have an array of None)
    for job in jobs:
        job.submit()
        job_module_dict[job.module] = job.jobid

    log.info("Submitted %s jobs" % len(jobs))
    log.info("Still %s left to be submitted" % len(with_dependencies))

    # dependencies have already been resolved this means i can linearly walk over the list and use previous job id's
    for pkg in with_dependencies:
        # don't forgot to prepare each package
        prepare_package(pkg)

        # the new job will only depend on already submitted jobs
        new_job = create_job(pkg, output_dir, script_dir)
        job_deps = [job_module_dict[dep] for dep in pkg['unresolvedDependencies']]
        new_job.add_dependencies(job_deps)
        new_job.submit()
        # update dictionary
        job_module_dict[new_job.module] = new_job.jobid

def aggregate_xml_in_dirs(base_dir, output_filename):
    """
    finds all the xml files in the dirs and takes the testcase attribute out of them.
    These are then put in a single output file
    """
    dom = xml.getDOMImplementation()
    root = dom.createDocument(None, "testsuite", None)
    properties = root.createElement("properties")
    version = root.createElement("property")
    version.setAttribute("name", "easybuild-version")
    version.setAttribute("value", str(easybuild.VERBOSE_VERSION))
    properties.appendChild(version)

    time_el = root.createElement("property")
    time_el.setAttribute("name", "timestamp")
    time_el.setAttribute("value", str(datetime.now()))
    properties.appendChild(time_el)

    root.firstChild.appendChild(properties)

    dirs = filter(os.path.isdir, [os.path.join(base_dir, dir) for dir in os.listdir(base_dir)])

    for dir in dirs:
        xml_file = glob.glob(os.path.join(dir, "*.xml"))
        if xml_file:
            # take the first one (should be only one present)
            xml_file = xml_file[0]
            dom = xml.parse(xml_file)
            # only one should be present, we are just discarding the rest
            testcase = dom.getElementsByTagName("testcase")[0]
            root.firstChild.appendChild(testcase)

    output_file = open(output_filename, "w")
    root.writexml(output_file)
    output_file.close()


def prepare_package(pkg):
    """ prepare for building """
    try:
        instance = get_instance(pkg)
        instance.prepare_build()
    except EasyBuildError, err:
        log.warn("%s failed to prepare. Submitting anyway, for proper error resolution" % str(pkg['module']))

def create_job(package, output_dir, script_dir):
    """
    creates a job, to build a *single* package
    returns the job
    """
    # command is pyton script/regtest.py file_name --single
    command = "cd %s && python %s %s --no-parallel" % (script_dir, sys.argv[0], package['spec'])

    # capture PYTHONPATH, MODULEPATH and all variables starting with EASYBUILD
    easybuild_vars = {}
    for name in os.environ:
        if name.startswith("EASYBUILD"):
            easybuild_vars[name] = os.environ[name]

    others = ["PYTHONPATH", "MODULEPATH"]

    for env_var in others:
        if env_var in os.environ:
            easybuild_vars[env_var] = os.environ[env_var]

    # create unique name based on module name
    name = "%s-%s" % package['module']

    easybuild_vars['EASYBUILDTESTOUTPUT'] = os.path.join(os.path.abspath(output_dir), name)

    job = PbsJob(command, name, easybuild_vars)
    job.module = package['module']

    return job


def write_to_xml(succes, failed, filename):
    """
    Create xml output, using minimal output required according to
    http://stackoverflow.com/questions/4922867/junit-xml-format-specification-that-hudson-supports
    """
    dom = xml.getDOMImplementation()
    root = dom.createDocument(None, "testsuite", None)

    def create_testcase(name):
        el = root.createElement("testcase")
        el.setAttribute("name", name)
        return el

    def create_failure(name, error_type, error):
        el = create_testcase(name)

        # encapsulate in CDATA section
        error_text = root.createCDATASection("\n%s\n" % error)
        failure_el = root.createElement("failure")
        failure_el.setAttribute("type", error_type)
        el.appendChild(failure_el)
        el.lastChild.appendChild(error_text)
        return el

    def create_succes(name, stats):
        el = create_testcase(name)
        text = "\n".join(["%s=%s" % (key, value) for (key, value) in stats.items()])
        build_stats = root.createCDATASection("\n%s\n" % text)
        system_out = root.createElement("system-out")
        el.appendChild(system_out)
        el.lastChild.appendChild(build_stats)
        return el

    properties = root.createElement("properties")
    version = root.createElement("property")
    version.setAttribute("name", "easybuild-version")
    version.setAttribute("value", str(easybuild.VERBOSE_VERSION))
    properties.appendChild(version)

    time = root.createElement("property")
    time.setAttribute("name", "timestamp")
    time.setAttribute("value", str(datetime.now()))
    properties.appendChild(time)

    root.firstChild.appendChild(properties)

    for (obj, fase, error) in failed:
        # try to pretty print
        try:
            el = create_failure("%s/%s" % (obj.name(), obj.installversion()), fase, error)
        except:
            el = create_failure(obj, fase, error)

        root.firstChild.appendChild(el)

    for (obj, stats) in succes:
        el = create_succes("%s/%s" % (obj.name(), obj.installversion()), stats)
        root.firstChild.appendChild(el)

    output_file = open(filename, "w")
    root.writexml(output_file, addindent="\t", newl="\n")
    output_file.close()

if __name__ == "__main__":
    main()
