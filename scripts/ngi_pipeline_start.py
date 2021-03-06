#!/bin/env python
""" Main entry point for the ngi_pipeline.

It can either start the Tornado server that will trigger analysis on the processing
cluster (UPPMAX for NGI), or trigger analysis itself.
"""
from __future__ import print_function

import argparse
import inflect
import os
import sys

from ngi_pipeline.conductor import flowcell
from ngi_pipeline.conductor import launchers
from ngi_pipeline.conductor.flowcell import organize_projects_from_flowcell, \
                                            setup_analysis_directory_structure
from ngi_pipeline.database.filesystem import create_charon_entries_from_project
from ngi_pipeline.engines import qc_ngi
from ngi_pipeline.log.loggers import minimal_logger
from ngi_pipeline.server import main as server_main
from ngi_pipeline.utils.filesystem import locate_project, recreate_project_from_filesystem

LOG = minimal_logger("ngi_pipeline_start")
inflector = inflect.engine()

def validate_dangerous_user_thing(action=("do SOMETHING that Mario thinks you "
                                          "should BE WARNED about"),
                                  setting_name=None,
                                  warning=None):
    if warning:
        print(warning, file=sys.stderr)
    else:
        print("WARNING: you have told this script to {action}! "
              "Are you sure??".format(action=action), file=sys.stderr)
    attempts = 0
    return_value = False
    while not return_value:
        if attempts < 3:
            attempts += 1
            user_input = raw_input("Confirm by typing 'yes' or 'no' "
                                   "({}): ".format(attempts)).lower()
            if user_input not in ('yes', 'no'):
                continue
            elif user_input == 'yes':
                return_value = True
            elif user_input == 'no':
                break
    if return_value:
        print("Confirmed!\n----", file=sys.stderr)
        return True
    else:
        message = "No confirmation received; "
        if setting_name:
            message += "setting {} to False.".format(setting_name)
        else:
            message += "not proceeding with action."
        message += "\n----"
        print(message, file=sys.stderr)
        return False


class ArgumentParserWithTheFlagsThatIWant(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super(ArgumentParserWithTheFlagsThatIWant, self).__init__(*args,
                formatter_class=argparse.ArgumentDefaultsHelpFormatter, **kwargs)
        self.add_argument("-f", "--restart-failed", dest="restart_failed_jobs", action="store_true",
                help=("Restart jobs marked as 'FAILED' in Charon"))
        self.add_argument("-d", "--restart-done", dest="restart_finished_jobs", action="store_true",
                help=("Restart jobs marked as DONE in Charon."))
        self.add_argument("-r", "--restart-running", dest="restart_running_jobs", action="store_true",
                help=("Restart jobs marked as UNDER_ANALYSIS in Charon. Use with care."))
        self.add_argument("-a", "--restart-all", dest="restart_all_jobs", action="store_true",
                help=("Just start any kind of job you can get your hands on regardless of status."))
        self.add_argument("-s", "--sample", dest="restrict_to_samples", action="append",
                help=("Restrict analysis to these samples. "
                      "Use flag multiple times for multiple samples."))

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Launch NGI pipeline")
    subparsers = parser.add_subparsers(help="Choose the mode to run")
    parser.add_argument("-v", "--verbose", dest="quiet", action="store_false",
            help=("Send mails (INFO/WARN/ERROR); default False."))

    # Add subparser for the server
    parser_server = subparsers.add_parser('server', help="Start ngi_pipeline server")
    parser_server.add_argument('-p', '--port', type=int,
            help="Port on which to listen for incoming connections")


    # Add subparser for organization
    parser_organize = subparsers.add_parser('organize',
            help="Organize one or more demultiplexed flowcells into project/sample/libprep/seqrun format.")
    subparsers_organize = parser_organize.add_subparsers(help='Choose unit to analyze')
    # Add sub-subparser for flowcell organization
    organize_flowcell = subparsers_organize.add_parser('flowcell',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='Organize one or more demultiplexed flowcells, populating Charon with relevant data.')
    organize_flowcell.add_argument("organize_fc_dirs", nargs="+",
            help=("The paths to the Illumina demultiplexed fc directories to organize"))
    organize_flowcell.add_argument("-l", "--fallback-libprep", default=None,
            help=("If no libprep is supplied in the SampleSheet.csv or in Charon, "
                  "use this value when creating records in Charon. (Optional)"))
    organize_flowcell.add_argument("-w", "--sequencing-facility", default="NGI-S", choices=('NGI-S', 'NGI-U'),
            help="The facility where sequencing was performed.")
    organize_flowcell.add_argument("-b", "--best_practice_analysis", default="whole_genome_reseq",
            help="The best practice analysis to run for this project or projects.")
    organize_flowcell.add_argument("-f", "--force", dest="force_update", action="store_true",
            help="Force updating Charon projects. Danger danger danger. This will overwrite things.")
    organize_flowcell.add_argument("-d", "--delete", dest="delete_existing", action="store_true",
            help="Delete existing projects in Charon. Similarly dangerous.")
    organize_flowcell.add_argument("--force-create-project", action="store_true",
            help="TESTING ONLY: Create a project if it does not exist in Charon using the project name as the project id.")
    organize_flowcell.add_argument("-s", "--sample", dest="restrict_to_samples", action="append",
            help="Restrict processing to these samples. Use flag multiple times for multiple samples.")
    organize_flowcell.add_argument("-p", "--project", dest="restrict_to_projects", action="append",
            help="Restrict processing to these projects. Use flag multiple times for multiple projects.")


    # Add subparser for analysis
    parser_analyze = subparsers.add_parser('analyze', help="Launch analysis.")
    subparsers_analyze = parser_analyze.add_subparsers(parser_class=ArgumentParserWithTheFlagsThatIWant,
            help='Choose unit to analyze')
    # Add sub-subparser for flowcell analysis
    analyze_flowcell = subparsers_analyze.add_parser('flowcell',
            help='Start analysis of raw flowcells')
    analyze_flowcell.add_argument("analyze_fc_dirs", nargs="+",
            help=("The path to one or more demultiplexed Illumina flowcell "
                  "directories to process and analyze."))
    analyze_flowcell.add_argument("-p", "--project", dest="restrict_to_projects", action="append",
            help=("Restrict analysis to these projects. "
                  "Use flag multiple times for multiple projects."))
    # Add sub-subparser for project analysis
    analyze_project = subparsers_analyze.add_parser('project',
            help='Start the analysis of a pre-parsed project.')
    analyze_project.add_argument('analyze_project_dirs', nargs='+',
            help='The path to the project folder to be analyzed.')

    # Add subparser for qc
    parser_qc = subparsers.add_parser('qc', help='Launch QC analysis.')
    subparsers_qc = parser_qc.add_subparsers(help='Choose unit to analyze')
    # Add sub-subparser for flowcell qc
    qc_flowcell = subparsers_qc.add_parser('flowcell',
            help='Start QC analysis of raw flowcells.')
    qc_flowcell.add_argument("-r", "--rerun", action="store_true",
            help='Force the rerun of the qc analysis if output files already exist.')
    qc_flowcell.add_argument("-l", "--fallback-libprep", default=None,
            help=("If no libprep is supplied in the SampleSheet.csv or in Charon, "
                  "use this value when creating records in Charon. (Optional)"))
    qc_flowcell.add_argument("-w", "--sequencing-facility", default="NGI-S", choices=('NGI-S', 'NGI-U'),
            help="The facility where sequencing was performed.")
    qc_flowcell.add_argument("-b", "--best_practice_analysis", default="whole_genome_reseq",
            help="The best practice analysis to run for this project or projects.")
    qc_flowcell.add_argument("-f", "--force", dest="force_update", action="store_true",
            help="Force updating Charon projects. Danger danger danger. This will overwrite things.")
    qc_flowcell.add_argument("-d", "--delete", dest="delete_existing", action="store_true",
            help="Delete existing projects in Charon. Similarly dangerous.")
    qc_flowcell.add_argument("-s", "--sample", dest="restrict_to_samples", action="append",
            help=("Restrict analysis to these samples. Use flag multiple times for multiple samples."))
    qc_flowcell.add_argument("-p", "--project", dest="restrict_to_projects", action="append",
            help="Restrict processing to these projects. Use flag multiple times for multiple projects.")
    qc_flowcell.add_argument("qc_flowcell_dirs", nargs="+",
            help=("The path to one or more demultiplexed Illumina flowcell "
                  "directories to process and run through QC analysis."))
    # Add sub-subparser for project qc
    qc_project = subparsers_qc.add_parser('project',
            help='Start QC analysis of a pre-parsed project directory.')
    qc_project.add_argument("-f", "--force-rerun", action="store_true",
            help='Force the rerun of the qc analysis if output files already exist.')
    qc_project.add_argument("-s", "--sample", dest="restrict_to_samples", action="append",
            help=("Restrict analysis to these samples. Use flag multiple times for multiple samples."))
    qc_project.add_argument("qc_project_dirs", nargs="+",
            help=("The path to one or more pre-parsed project directories to "
                  "run through QC analysis."))


    # Add subparser for genotyping
    parser_genotype = subparsers.add_parser('genotype', help="Launch genotype concordance analysis.")
    subparsers_genotype = parser_genotype.add_subparsers(parser_class=ArgumentParserWithTheFlagsThatIWant,
            help="Choose unit to analyze.")
    # Add sub-subparser for project genotyping
    genotype_project = subparsers_genotype.add_parser('project',
            help="Start genotype analysis for all samples in a project")
    genotype_project.add_argument("genotype_project_dirs", nargs="+",
            help=("The path to one or more pre-parsed project directories to "
                  "run through genotype concordance analysis."))
    genotype_project.add_argument("-g", "--genotype-file", action="store", required=True,
            help="The path to the genotype VCF file.")
    # Add sub-subparser for sample genotyping
    #genotype_sample = subparsers_genotype.add_parser('sample',
    #        help="Start genotype analysis for one specific sample in a project.")


    args = parser.parse_args()

    # These options are available only if the script has been called with the 'analyze' option
    if args.__dict__.get('restart_all_jobs'):
        if validate_dangerous_user_thing(action=("restart all FAILED, RUNNING, "
                                                 "and FINISHED jobs, deleting "
                                                 "previous analyses")):
            args.restart_failed_jobs = True
            args.restart_finished_jobs = True
            args.restart_running_jobs = True
    else:
        if args.__dict__.get("restart_failed_jobs"):
            args.restart_failed_jobs = \
                validate_dangerous_user_thing(action=("restart FAILED jobs, deleting "
                                                      "previous analysies files"))
        if args.__dict__.get("restart_finished_jobs"):
            args.restart_finished_jobs = \
                validate_dangerous_user_thing(action=("restart FINISHED jobs, deleting "
                                                      "previous analyseis files"))
        if args.__dict__.get("restart_running_jobs"):
            args.restart_finished_jobs = \
                validate_dangerous_user_thing(action=("restart RUNNING jobs, deleting "
                                                      "previous analysis files"))
    # Charon-specific arguments ('organize', 'analyze', 'qc')
    if args.__dict__.get("force_update"):
        args.force_update = \
                validate_dangerous_user_thing("overwrite existing data in Charon")
    if args.__dict__.get("delete_existing"):
        args.delete_existing = \
                validate_dangerous_user_thing("delete existing data in Charon")


    # Finally execute corresponding functions
    if 'analyze_fc_dirs' in args:
        LOG.info('Starting flowcell analysis of flowcell {} '
                 '{}'.format(inflector.plural("directory", len(args.analyze_fc_dirs)),
                             ", ".join(args.analyze_fc_dirs)))
        flowcell.process_demultiplexed_flowcells(args.analyze_fc_dirs,
                                                 args.restrict_to_projects,
                                                 args.restrict_to_samples,
                                                 args.restart_failed_jobs,
                                                 args.restart_finished_jobs,
                                                 args.restart_running_jobs,
                                                 quiet=args.quiet,
                                                 manual=True)

    elif 'analyze_project_dirs' in args:
        for analyze_project_dir in args.analyze_project_dirs:
            try:
                project_dir = locate_project(analyze_project_dir)
            except ValueError as e:
                LOG.error(e)
                continue
            project = recreate_project_from_filesystem(project_dir=project_dir,
                                                       restrict_to_samples=args.restrict_to_samples)
            if project and os.path.split(project.base_path)[1] == "DATA":
                project.base_path = os.path.split(project.base_path)[0]
            launchers.launch_analysis([project],
                                      restart_failed_jobs=args.restart_failed_jobs,
                                      restart_finished_jobs=args.restart_finished_jobs,
                                      restart_running_jobs=args.restart_running_jobs,
                                      quiet=args.quiet,
                                      manual=True)

    elif 'qc_flowcell_dirs' in args:
        qc_flowcell_dirs_list = list(set(args.qc_flowcell_dirs))
        LOG.info("Organizing flowcell {} {}".format(inflector.plural("directory",
                                                                     len(qc_flowcell_dirs_list)),
                                                    ", ".join(qc_flowcell_dirs_list)))
        projects_to_analyze = \
                organize_projects_from_flowcell(demux_fcid_dirs=qc_flowcell_dirs_list,
                                                restrict_to_projects=args.restrict_to_projects,
                                                restrict_to_samples=args.restrict_to_samples,
                                                fallback_libprep=args.fallback_libprep,
                                                quiet=args.quiet)
        for project in projects_to_analyze:
            try:
                create_charon_entries_from_project(project=project,
                                                   best_practice_analysis=args.best_practice_analysis,
                                                   sequencing_facility=args.sequencing_facility,
                                                   force_overwrite=args.force_update,
                                                   delete_existing=args.delete_existing)
            except Exception as e:
                print(e, file=sys.stderr)
        LOG.info("Done with organization.")
        for project in projects_to_analyze:
            for sample in project:
                qc_ngi.launchers.analyze(project, sample, quiet=args.quiet)

    elif 'qc_project_dirs' in args:
        for qc_project_dir in args.qc_project_dirs:
            project = recreate_project_from_filesystem(project_dir=qc_project_dir,
                                                       restrict_to_samples=args.restrict_to_samples)
            if project and os.path.split(project.base_path)[1] == "DATA":
                project.base_path = os.path.split(project.base_path)[0]
            if not project.samples:
                LOG.info('No samples found for project {} (path {})'.format(project.project_id,
                                                                            qc_project_dir))
            for sample in project:
                qc_ngi.launchers.analyze(project, sample, quiet=args.quiet)

    elif 'organize_fc_dirs' in args:
        organize_fc_dirs_list = list(set(args.organize_fc_dirs))
        LOG.info("Organizing flowcell {} {}".format(inflector.plural("directory",
                                                                     len(organize_fc_dirs_list)),
                                                    ", ".join(organize_fc_dirs_list)))
        projects_to_analyze = \
                organize_projects_from_flowcell(demux_fcid_dirs=organize_fc_dirs_list,
                                                restrict_to_projects=args.restrict_to_projects,
                                                restrict_to_samples=args.restrict_to_samples,
                                                fallback_libprep=args.fallback_libprep,
                                                quiet=args.quiet)
        for project in projects_to_analyze:
            try:
                create_charon_entries_from_project(project=project,
                                                   best_practice_analysis=args.best_practice_analysis,
                                                   sequencing_facility=args.sequencing_facility,
                                                   force_overwrite=args.force_update,
                                                   delete_existing=args.delete_existing)
            except Exception as e:
                print(e, file=sys.stderr)
        LOG.info("Done with organization.")

    elif 'genotype_project_dirs' in args:
        from ngi_pipeline.engines import piper_ngi
        genotype_file_path = args.genotype_file
        for genotype_project_dir in args.genotype_project_dirs:
            LOG.info("Starting genotype analysis of project {} with genotype "
                     "file {}".format(genotype_project_dir, genotype_file_path))
            project = recreate_project_from_filesystem(project_dir=genotype_project_dir,
                                                       restrict_to_samples=args.restrict_to_samples)
            if project and os.path.split(project.base_path)[1] == "DATA":
                project.base_path = os.path.split(project.base_path)[0]
            for sample in project:
                piper_ngi.launchers.genotype(project, sample,
                                             genotype_file=genotype_file_path,
                                             restart_finished_jobs=args.restart_finished_jobs,
                                             restart_running_jobs=args.restart_running_jobs,
                                             level="genotype")

    elif 'port' in args:
        LOG.info('Starting ngi_pipeline server at port {}'.format(args.port))
        server_main.start(args.port)
