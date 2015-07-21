#!/usr/bin/env python

import re
import os
import json
import argparse
import synqueue
import shutil
import synapseclient
import subprocess
from math import isnan
from nebula.service import GalaxyService
from nebula.galaxy import GalaxyWorkflow
from nebula.docstore import from_url
from nebula.target import Target
from nebula.tasks import TaskGroup, GalaxyWorkflowTask
import tempfile
import datetime
from urlparse import urlparse

REFDATA_PROJECT="syn3241088"

config = {
  #"table_id" : "syn3498886",
  #"state_col" : "Processing State",
  #"primary_col" : "Donor_ID",
  "table_id" : "syn4556289",
  "primary_col" : "Submitter_donor_ID",
  "assignee_col" : "Assignee",
  "state_col" : "State"
}

key_map = {
    "cghub.ucsc.edu" : "/tool_data/files/cghub.key",
    "gtrepo-ebi.annailabs.com" : "/tool_data/files/icgc.key",
    "gtrepo-bsc.annailabs.com" : "/tool_data/files/icgc.key",
    "gtrepo-osdc-icgc.annailabs.com" : "/tool_data/files/icgc.key",
    "gtrepo-riken.annailabs.com" : "/tool_data/files/icgc.key",
    "gtrepo-dkfz.annailabs.com" : "/tool_data/files/icgc.key",
    "gtrepo-etri.annailabs.com" : "/tool_data/files/icgc.key",
}

def run_gen(args):
    args = parser.parse_args()

    syn = synapseclient.Synapse()
    syn.login()

    docstore = from_url(args.out_base)

    if args.ref_download:
        #download reference files from Synapse and populate the document store
        for a in syn.chunkedQuery('select * from entity where parentId=="%s"' % (REFDATA_PROJECT)):
            ent = syn.get(a['entity.id'])

            id = ent.annotations['uuid'][0]
            t = Target(uuid=id)
            docstore.create(t)
            path = docstore.get_filename(t)
            name = ent.name
            if 'dataPrep' in ent.annotations:
                if ent.annotations['dataPrep'][0] == 'gunzip':
                    subprocess.check_call("gunzip -c %s > %s" % (ent.path, path), shell=True)
                    name = name.replace(".gz", "")
                else:
                    print "Unknown DataPrep"
            else:
                shutil.copy(ent.path, path)
            docstore.update_from_file(t)
            meta = {}
            meta['name'] = name
            meta['uuid'] = id
            if 'dataPrep' in meta:
                del meta['dataPrep']
            docstore.put(id, meta)

    data_mapping = {
        "reference_genome" : "genome.fa",
        "dbsnp" : "dbsnp_132_b37.leftAligned.vcf",
        "cosmic" : "b37_cosmic_v54_120711.vcf",
        "gold_indels" : "Mills_and_1000G_gold_standard.indels.hg19.sites.fixed.vcf",
        "phase_one_indels" : "1000G_phase1.indels.hg19.sites.fixed.vcf",
        "centromere" : "centromere_hg19.bed"
    }

    dm = {}
    for k,v in data_mapping.items():
        hit = None
        for a in docstore.filter(name=v):
            hit = a[0]
        if hit is None:
            raise Exception("%s not found" % (v))
        dm[k] = { "uuid" : hit }

    workflow = GalaxyWorkflow(ga_file="workflows/Galaxy-Workflow-PCAWG_CGHUB.ga")
    tasks = TaskGroup()
    for ent in synqueue.listAssignments(syn, **config):
        #print "'%s'" % (ent['state']), ent['state'] == 'nan', type(ent['state']), type('nan')
        if not isinstance(ent['state'], basestring) and isnan(ent['state']):
            gnos_endpoint = urlparse(ent['meta']['Normal_WGS_alignment_GNOS_repos']).netloc
            task = GalaxyWorkflowTask("workflow_%s" % (ent['id']),
                workflow,
                inputs=dm,
                parameters={
                    'normal_bam_download' : {
                        "uuid" : ent['meta']['Normal_WGS_alignment_GNOS_analysis_ID'],
                        "gnos_endpoint" : gnos_endpoint,
                        "cred_file" : key_map[gnos_endpoint]
                    },
                    'tumor_bam_download' : {
                        "uuid" : ent['meta']['Tumour_WGS_alignment_GNOS_analysis_IDs'],
                        "gnos_endpoint" : gnos_endpoint,
                        "cred_file" : key_map[gnos_endpoint]
                    },
                    'broad_variant_pipeline' : {
                        "broad_ref_dir" : "/tool_data/files/refdata",
                        "sample_id" : ent['meta']['Submitter_donor_ID']
                    }
                },
                tags=[ "donor:%s" % (ent['meta']['Submitter_donor_ID']) ]
            )
            tasks.append(task)

    if not os.path.exists("%s.tasks" % (args.out_base)):
        os.mkdir("%s.tasks" % (args.out_base))

    for data in tasks:
        with open("%s.tasks/%s" % (args.out_base, data.task_id), "w") as handle:
            handle.write(json.dumps(data.to_dict()))
    
    print "Tasks Created: %s" % (len(tasks))

    if args.create_service:
        service = GalaxyService(
            docstore=docstore,
            galaxy="bgruening/galaxy-stable",
            sudo=True,
            tool_data=os.path.abspath("tool_data"),
            tool_dir=os.path.abspath("tools"),
            smp=[
                ["MuSE", 8],
                ["pindel", 8],
                ["muTect", 8],
                ["delly", 4],
                ["gatk_bqsr", 12],
                ["gatk_indel", 24],
                ["bwa_mem", 12],
                ["broad_variant_pipline", 24]
            ]
        )
        with open("%s.service" % (args.out_base), "w") as handle:
            s = service.get_config()
            if args.scratch:
                print "Using scratch", args.scratch
                s.set_docstore_config(cache_path=args.scratch, open_perms=True)
            s.store(handle)

def run_audit(args):
    args = parser.parse_args()

    syn = synapseclient.Synapse()
    syn.login()

    docstore = from_url(args.out_base)

    donor_map = {}
    for id, ent in docstore.filter( state="ok" ):
        if ent['visible']:
            if docstore.size(Target(id)) > 0:
                donor = None
                for i in ent['tags']:
                    t = i.split(":")
                    if t[0] == "donor":
                        donor = t[1]
                if donor not in donor_map:
                    donor_map[donor] = {}
                donor_map[donor][ent['name']] = id
    
    for ent in synqueue.listAssignments(syn, list_all=True, **config):
        if ent['meta']['Submitter_donor_ID'] in donor_map:
            print ent['meta']['Submitter_donor_ID'], len(donor_map[ent['meta']['Submitter_donor_ID']])

def run_register(args):

    syn = synapseclient.Synapse()
    syn.login()
    synqueue.registerAssignments(syn, args.count, display=True, force=args.force, **config)

def run_uploadprep(args):
    
    if not os.path.exists(args.workdir):
        os.mkdir(args.workdir)
    doc = from_url(args.out_base)
    file_map = {
        'broad' : {},
        'muse' : {}
    }

    syn = synapseclient.Synapse()
    syn.login()

    wl_map = {}
    job_map = {}
    for ent in synqueue.listAssignments(syn, list_all=True, **config):
        wl_map[ent['id']] = ent['meta']
    
    for id, entry in doc.filter():
        donor = None    
        if 'tags' in entry:
            for s in entry['tags']:
                tmp = s.split(":")
                if tmp[0] == 'donor':
                    donor = tmp[1]
        if donor is not None: 
            if donor not in job_map:
                job_map[donor] = {}
            if 'job' in entry and 'job_metrics' in entry['job']:
                print entry['name']
                for met in entry['job']['job_metrics']:
                    if met['name'] == 'runtime_seconds':
                        job_map[donor][entry['name']] = {"tool_id" : entry['job']['tool_id'], "runtime_seconds" : met['raw_value']}
            if entry.get('visible', False) and entry.get('extension', None) in ["vcf", "vcf_bgzip"]:
                pipeline = None
                method = None
                call_type = None
                variant_type = None
                if entry['name'].split('.')[0] in ['MUSE_1']:
                    pipeline = "muse"
                    method = entry['name'].replace(".", "-")
                    variant_type = 'somatic'
                    call_type = 'snv_mnv'
                elif entry['name'].split(".")[0] in ['broad-dRanger', 'broad-dRanger_snowman', 'broad-snowman', 'broad-mutect' ]:
                    pipeline = "broad"
                    method = entry['name'].split(".")[0]
                    if 'somatic' in entry['name']:
                        variant_type = 'somatic'
                    elif 'germline' in entry['name']:
                        variant_type = 'germline'
                    else:
                        raise Exception("Unknown variant type")
                    if 'snv_mnv.vcf' in entry['name']:
                        call_type = 'snv_mnv'
                    elif 'sv.vcf' in entry['name']:
                        call_type = 'sv'
                    elif 'indel.vcf' in entry['name']:
                        call_type = 'indel'
                    else:
                        raise Exception("Unknown call type: %s" % (entry['name']))
                else:
                    raise Exception("Unknown pipeline %s" % (entry['name']))

                datestr = datetime.datetime.now().strftime("%Y%m%d")
                name = "%s.%s.%s.%s.%s" % (donor, method, datestr, variant_type, call_type )

                name = re.sub(r'.vcf$', '', name)
                if entry['extension'] == 'vcf':
                    file_name = name + ".vcf"
                elif entry['extension'] == 'vcf_bgzip':
                    file_name = name + ".vcf.gz"
                target = Target(uuid=entry['uuid'])
                if doc.size(target) > 0:
                    src_file = doc.get_filename(target)
                    dst_file = os.path.join(args.workdir, file_name)

                    shutil.copy(src_file, dst_file)
                    if entry['extension'] == 'vcf':
                        subprocess.check_call( "bgzip -c %s > %s.gz" % (dst_file, dst_file), shell=True )
                        dst_file = dst_file + ".gz"

                    subprocess.check_call("tabix -p vcf %s" % (dst_file), shell=True)
                    shutil.move("%s.tbi" % (dst_file), "%s.idx" % (dst_file))
                    subprocess.check_call("md5sum %s | awk '{print$1}' > %s.md5" % (dst_file, dst_file), shell=True)
                    subprocess.check_call("md5sum %s.idx | awk '{print$1}' > %s.idx.md5" % (dst_file, dst_file), shell=True)

                    if donor not in file_map[pipeline]:
                        file_map[pipeline][donor] = []

                    input_file = os.path.basename(dst_file)
                    file_map[pipeline][donor].append(input_file)

    for pipeline, donors in file_map.items():
        for donor, files in donors.items():
            if donor in wl_map:
                """
                with open( os.path.join(args.workdir, "%s.%s.pipeline.json" %(pipeline, donor)), "w" ) as handle:
                    handle.write(json.dumps( {"pipeline_src" : args.pipeline_src, "pipeline_version" : args.pipeline_version} ))
                """
                timing_json = os.path.join(args.workdir, "%s.%s.timing.json" %(pipeline, donor))
                with open( timing_json, "w" ) as handle:
                    handle.write(json.dumps( job_map[donor] ) )
                    
            
                with open( os.path.join(args.workdir, "%s.%s.sh" %(pipeline, donor)), "w" ) as handle:
                    input_file = os.path.basename(dst_file)
                    urls = [
                        "%scghub/metadata/analysisFull/%s" % (wl_map[donor]['Normal_WGS_alignment_GNOS_repos'], wl_map[donor]['Normal_WGS_alignment_GNOS_analysis_ID']),
                        "%scghub/metadata/analysisFull/%s" % (wl_map[donor]['Tumour_WGS_alignment_GNOS_repos'], wl_map[donor]['Tumour_WGS_alignment_GNOS_analysis_IDs'])
                    ]
                    cmd_str = "perl /opt/vcf-uploader/gnos_upload_vcf.pl"
                    cmd_str += " --metadata-urls %s" % (",".join(urls))
                    cmd_str += " --vcfs %s " % (",".join(files))
                    cmd_str += " --vcf-md5sum-files %s " % ((",".join( ("%s.md5" % i for i in files) )))
                    cmd_str += " --vcf-idxs %s" % ((",".join( ("%s.idx" % i for i in files) )))
                    cmd_str += " --vcf-idx-md5sum-files %s" % ((",".join( ("%s.idx.md5" % i for i in files) )))
                    cmd_str += " --outdir %s.%s.dir" % (pipeline, donor)
                    cmd_str += " --key %s " % (args.keyfile)
                    cmd_str += " --upload-url %s" % (args.upload_url)
                    cmd_str += " --study-refname-override tcga_pancancer_vcf_test"
                    cmd_str += " --workflow-src-url '%s'" % args.pipeline_src
                    cmd_str += " --timing-metrics-json %s" % (timing_json)
                    handle.write("#!/bin/bash\n%s\n" % (cmd_str) )


def run_list(args):
    syn = synapseclient.Synapse()
    syn.login()
    synqueue.listAssignments(syn, display=True, **config)
    
def run_set(args):
    syn = synapseclient.Synapse()
    syn.login()
    
    synqueue.setStates(syn, args.state, args.ids, **config)
    


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers( title='commands')

    parser_gen = subparsers.add_parser('gen')
    parser_gen.add_argument("--out-base", default="pcawg_data")
    parser_gen.add_argument("--ref-download", action="store_true", default=False)
    parser_gen.add_argument("--create-service", action="store_true", default=False)
    parser_gen.add_argument("--scratch", default=None)
    parser_gen.add_argument("--work-dir", default=None)
    parser_gen.add_argument("--tool-data", default=os.path.abspath("tool_data"))
    parser_gen.add_argument("--tool-dir", default=os.path.abspath("tools"))
    
    parser_gen.set_defaults(func=run_gen)

    parser_submit = subparsers.add_parser('audit')
    parser_submit.add_argument("--out-base", default="pcawg_data")
    parser_submit.set_defaults(func=run_audit)

    parser_register = subparsers.add_parser('register',
                                       help='Returns set of new assignments')
    parser_register.add_argument("-c", "--count", help="Number of assignments to get", type=int, default=1)
    parser_register.add_argument("-f", "--force", help="Force Register specific ID", default=None)
    parser_register.set_defaults(func=run_register)
    
    parser_upload = subparsers.add_parser('upload-prep')
    parser_upload.add_argument("--workdir", default="work")
    parser_upload.add_argument("--keyfile", default="cghub.pem")
    parser_upload.add_argument("--upload_url", default="https://gtrepo-osdc-tcga.annailabs.com")
    parser_upload.add_argument("--out-base", default="pcawg_data")
    parser_upload.add_argument("--pipeline-src", default="https://github.com/ucscCancer/pcawg_tools")
    parser_upload.add_argument("--pipeline-version", default="1.0.0")
    parser_upload.set_defaults(func=run_uploadprep)

    parser_list = subparsers.add_parser('list')
    parser_list.set_defaults(func=run_list)
    
    parser_set = subparsers.add_parser('set')
    parser_set.add_argument("state")
    parser_set.add_argument("ids", nargs="+")
    parser_set.set_defaults(func=run_set)

    args = parser.parse_args()
    args.func(args)

