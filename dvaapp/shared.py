import os, json, requests, copy, time, subprocess, logging
from models import Video, TEvent,  Region, VDNDataset, VDNServer, VDNDetector, CustomDetector, DeletedVideo, Label,\
    RegionLabel
from django.conf import settings
from django_celery_results.models import TaskResult
from collections import defaultdict
from operations import processing
from operations.processing import get_queue_name
from dva.celery import app
from models import DVAPQL
from . import serializers
import boto3
from botocore.exceptions import ClientError


def refresh_task_status():
    for t in TEvent.objects.all().filter(started=True, completed=False, errored=False):
        try:
            tr = TaskResult.objects.get(task_id=t.task_id)
        except TaskResult.DoesNotExist:
            pass
        else:
            if tr.status == 'FAILURE':
                t.errored = True
                t.save()


def create_video_folders(video, create_subdirs=True):
    os.mkdir('{}/{}'.format(settings.MEDIA_ROOT, video.pk))
    if create_subdirs:
        os.mkdir('{}/{}/video/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/frames/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/segments/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/indexes/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/regions/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/transforms/'.format(settings.MEDIA_ROOT, video.pk))
        os.mkdir('{}/{}/audio/'.format(settings.MEDIA_ROOT, video.pk))


def create_detector_folders(detector, create_subdirs=True):
    try:
        os.mkdir('{}/detectors/{}'.format(settings.MEDIA_ROOT, detector.pk))
    except:
        pass


def create_indexer_folders(indexer, create_subdirs=True):
    try:
        os.mkdir('{}/indexers/{}'.format(settings.MEDIA_ROOT, indexer.pk))
    except:
        pass


def create_annotator_folders(annotator, create_subdirs=True):
    try:
        os.mkdir('{}/annotators/{}'.format(settings.MEDIA_ROOT, annotator.pk))
    except:
        pass


def delete_video_object(video_pk,deleter,garbage_collection=True):
    video = Video.objects.get(pk=video_pk)
    deleted = DeletedVideo()
    deleted.name = video.name
    deleted.deleter = deleter
    deleted.uploader = video.uploader
    deleted.url = video.url
    deleted.description = video.description
    deleted.original_pk = video_pk
    deleted.save()
    video.delete()
    if garbage_collection:
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {'video_pk': video.pk},
                    'operation': 'delete_video_by_id',
                }
            ]
        }
        p.create_from_json(j=query, user=deleter)
        p.launch()


def handle_uploaded_file(f, name, extract=True, user=None, rate=None, rescale=None):
    if rate is None:
        rate = settings.DEFAULT_RATE
    if rescale is None:
        rescale = settings.DEFAULT_RESCALE
    video = Video()
    if user:
        video.uploader = user
    video.name = name
    video.save()
    filename = f.name
    filename = filename.lower()
    if filename.endswith('.dva_export.zip'):
        create_video_folders(video, create_subdirs=False)
        with open('{}/{}/{}.{}'.format(settings.MEDIA_ROOT, video.pk, video.pk, filename.split('.')[-1]),
                  'wb+') as destination:
            for chunk in f.chunks():
                destination.write(chunk)
        video.uploaded = True
        video.save()
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {},
                    'video_id': video.pk,
                    'operation': 'import_video',
                }
            ]
        }
        p.create_from_json(j=query, user=user)
        p.launch()
    elif filename.endswith('.mp4') or filename.endswith('.flv') or filename.endswith('.zip'):
        create_video_folders(video, create_subdirs=True)
        with open('{}/{}/video/{}.{}'.format(settings.MEDIA_ROOT, video.pk, video.pk, filename.split('.')[-1]),
                  'wb+') as destination:
            for chunk in f.chunks():
                destination.write(chunk)
        video.uploaded = True
        if filename.endswith('.zip'):
            video.dataset = True
        video.save()
        if extract:
            p = processing.DVAPQLProcess()
            if video.dataset:
                query = {
                    'process_type':DVAPQL.PROCESS,
                    'tasks':[
                        {
                            'arguments':{'rate': rate, 'rescale': rescale,
                                         'frames_batch_size':settings.DEFAULT_FRAMES_BATCH_SIZE,
                                         'next_tasks':settings.DEFAULT_PROCESSING_PLAN},
                            'video_id':video.pk,
                            'operation': 'extract_frames',
                        }
                    ]
                }
            else:
                query = {
                    'process_type':DVAPQL.PROCESS,
                    'tasks':[
                        {
                            'arguments':{
                                'segments_batch_size': settings.DEFAULT_SEGMENTS_BATCH_SIZE,
                                'next_tasks':[
                                             {'operation':'decode_video',
                                               'arguments':{
                                                   'rate': rate,
                                                   'rescale': rescale,
                                                   'next_tasks':settings.DEFAULT_PROCESSING_PLAN
                                               }
                                              }
                                            ]},
                            'video_id':video.pk,
                            'operation': 'segment_video',
                        }
                    ]
                }
            p.create_from_json(j=query,user=user)
            p.launch()
    else:
        raise ValueError, "Extension {} not allowed".format(filename.split('.')[-1])
    return video


def handle_downloaded_file(downloaded, video, name):
    video.name = name
    video.save()
    filename = downloaded.split('/')[-1]
    if filename.endswith('.dva_export.zip'):
        create_video_folders(video, create_subdirs=False)
        os.rename(downloaded, '{}/{}/{}.{}'.format(settings.MEDIA_ROOT, video.pk, video.pk, filename.split('.')[-1]))
        video.uploaded = True
        video.save()
    elif filename.endswith('.mp4') or filename.endswith('.flv') or filename.endswith('.zip'):
        create_video_folders(video, create_subdirs=True)
        os.rename(downloaded,
                  '{}/{}/video/{}.{}'.format(settings.MEDIA_ROOT, video.pk, video.pk, filename.split('.')[-1]))
        video.uploaded = True
        if filename.endswith('.zip'):
            video.dataset = True
        video.save()
    else:
        raise ValueError, "Extension {} not allowed".format(filename.split('.')[-1])
    return video


def create_annotation(form, object_name, labels, frame):
    annotation = Region()
    annotation.object_name = object_name
    if form.cleaned_data['high_level']:
        annotation.full_frame = True
        annotation.x = 0
        annotation.y = 0
        annotation.h = 0
        annotation.w = 0
    else:
        annotation.full_frame = False
        annotation.x = form.cleaned_data['x']
        annotation.y = form.cleaned_data['y']
        annotation.h = form.cleaned_data['h']
        annotation.w = form.cleaned_data['w']
    annotation.text = form.cleaned_data['text']
    annotation.metadata = form.cleaned_data['metadata']
    annotation.frame = frame
    annotation.video = frame.video
    annotation.region_type = Region.ANNOTATION
    annotation.save()
    for lname in labels:
        if lname.strip():
            dl, _ = Label.objects.get_or_create(name=lname, set="UI")
            rl = RegionLabel()
            rl.video = annotation.video
            rl.frame = annotation.frame
            rl.region = annotation
            rl.label = dl
            rl.save()


def handle_youtube_video(name, url, extract=True, user=None, rate=30, rescale=0):
    video = Video()
    if user:
        video.uploader = user
    video.name = name
    video.url = url
    video.youtube_video = True
    video.save()
    if extract:
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {'next_tasks': [
                        {'operation': 'decode_video',
                         'arguments': {
                             'rate': rate,
                             'rescale': rescale,
                             'next_tasks': settings.DEFAULT_PROCESSING_PLAN
                         }
                         }
                    ]},
                    'video_id': video.pk,
                    'segments_batch_size': settings.DEFAULT_SEGMENTS_BATCH_SIZE,
                    'operation': 'segment_video',
                }
            ]
        }
        p.create_from_json(j=query, user=user)
        p.launch()
    return video


def create_child_vdn_dataset(parent_video, server, headers):
    server_url = server.url
    if not server_url.endswith('/'):
        server_url += '/'
    new_dataset = {'root': False,
                   'parent_url': parent_video.vdn_dataset.url,
                   'description': 'automatically created child'}
    r = requests.post("{}api/datasets/".format(server_url), data=new_dataset, headers=headers)
    if r.status_code == 201:
        vdn_dataset = VDNDataset()
        vdn_dataset.url = r.json()['url']
        vdn_dataset.root = False
        vdn_dataset.response = r.text
        vdn_dataset.server = server
        vdn_dataset.parent_local = parent_video.vdn_dataset
        vdn_dataset.save()
        return vdn_dataset
    else:
        raise ValueError, "{} {} {} {}".format("{}api/datasets/".format(server_url), headers, r.status_code,
                                               new_dataset)


def create_root_vdn_dataset(region, bucket, key, server, headers, name, description):
    new_dataset = {'root': True,
                   'aws_requester_pays': True,
                   'aws_region': region,
                   'aws_bucket': bucket,
                   'aws_key': key,
                   'name': name,
                   'description': description
                   }
    server_url = server.url
    if not server_url.endswith('/'):
        server_url += '/'
    r = requests.post("{}api/datasets/".format(server_url), data=new_dataset, headers=headers)
    if r.status_code == 201:
        vdn_dataset = VDNDataset()
        vdn_dataset.url = r.json()['url']
        vdn_dataset.root = True
        vdn_dataset.response = r.text
        vdn_dataset.server = server
        vdn_dataset.save()
        return vdn_dataset
    else:
        raise ValueError, "Could not crated dataset"


def pull_vdn_list(pk):
    """
    Pull list of datasets from configured VDN servers
    """
    server = VDNServer.objects.get(pk=pk)
    datasets = []
    detectors = []
    r = requests.get("{}vdn/api/datasets/".format(server.url))
    response = r.json()
    for d in response['results']:
        datasets.append(d)
    while response['next']:
        r = requests.get(response['next'])
        response = r.json()
        for d in response['results']:
            datasets.append(d)
    r = requests.get("{}vdn/api/detectors/".format(server.url))
    response = r.json()
    for d in response['results']:
        detectors.append(d)
    while response['next']:
        r = requests.get(response['next'])
        response = r.json()
        for d in response['results']:
            detectors.append(d)
    server.last_response_datasets = json.dumps(datasets)
    server.last_response_detectors = json.dumps(detectors)
    server.save()
    return server, datasets, detectors




def create_dataset(d, server):
    dataset = VDNDataset()
    dataset.server = server
    dataset.name = d['name']
    dataset.description = d['description']
    dataset.download_url = d['download_url']
    dataset.url = d['url']
    dataset.aws_bucket = d['aws_bucket']
    dataset.aws_key = d['aws_key']
    dataset.aws_region = d['aws_region']
    dataset.aws_requester_pays = d['aws_requester_pays']
    dataset.organization_url = d['organization']['url']
    dataset.response = json.dumps(d)
    dataset.save()
    return dataset


def import_vdn_dataset_url(server, url, user):
    r = requests.get(url)
    response = r.json()
    vdn_dataset = create_dataset(response, server)
    vdn_dataset.save()
    video = Video()
    if user:
        video.uploader = user
    video.name = vdn_dataset.name
    video.vdn_dataset = vdn_dataset
    video.save()
    if vdn_dataset.download_url:
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {},
                    'video_id': video.pk,
                    'operation': 'import_vdn_file',
                }
            ]
        }
        p.create_from_json(j=query, user=user)
        p.launch()
    elif vdn_dataset.aws_key and vdn_dataset.aws_bucket:
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {},
                    'video_id': video.pk,
                    'operation': 'import_vdn_s3',
                }
            ]
        }
        p.create_from_json(j=query, user=user)
        p.launch()
    else:
        raise NotImplementedError


def create_vdn_detector(d, server):
    vdn_detector = VDNDetector()
    vdn_detector.server = server
    vdn_detector.name = d['name']
    vdn_detector.description = d['description']
    vdn_detector.download_url = d['download_url']
    vdn_detector.url = d['url']
    vdn_detector.aws_bucket = d['aws_bucket']
    vdn_detector.aws_key = d['aws_key']
    vdn_detector.aws_region = d['aws_region']
    vdn_detector.aws_requester_pays = d['aws_requester_pays']
    vdn_detector.organization_url = d['organization']['url']
    vdn_detector.response = json.dumps(d)
    vdn_detector.save()
    return vdn_detector


def import_vdn_detector_url(server, url, user):
    r = requests.get(url)
    response = r.json()
    vdn_detector = create_vdn_detector(response, server)
    detector = CustomDetector()
    detector.name = vdn_detector.name
    detector.vdn_detector = vdn_detector
    detector.save()
    if vdn_detector.download_url:
        p = processing.DVAPQLProcess()
        query = {
            'process_type': DVAPQL.PROCESS,
            'tasks': [
                {
                    'arguments': {'detector_pk': detector.pk},
                    'operation': 'import_vdn_detector_file',
                }
            ]
        }
        p.create_from_json(j=query, user=user)
        p.launch()
    elif vdn_detector.aws_key and vdn_detector.aws_bucket:
        raise NotImplementedError
    else:
        raise NotImplementedError


def create_detector_dataset(object_names, labels):
    class_distribution = defaultdict(int)
    rboxes = defaultdict(list)
    rboxes_set = defaultdict(set)
    frames = {}
    class_names = {k: i for i, k in enumerate(labels.union(object_names))}
    i_class_names = {i: k for k, i in class_names.items()}
    for r in Region.objects.all().filter(object_name__in=object_names):
        frames[r.frame_id] = r.frame
        if r.pk not in rboxes_set[r.frame_id]:
            rboxes[r.frame_id].append((class_names[r.object_name], r.x, r.y, r.x + r.w, r.y + r.h))
            rboxes_set[r.frame_id].add(r.pk)
            class_distribution[r.object_name] += 1
    for dl in Label.objects.filter(name__in=labels):
        lname = dl.name
        for l in RegionLabel.all().objects.filter(label=dl):
            frames[l.frame_id] = l.frame
            if l.region:
                r = l.region
                if r.pk not in rboxes_set[r.frame_id]:
                    rboxes[l.frame_id].append((class_names[lname], r.x, r.y, r.x + r.w, r.y + r.h))
                    rboxes_set[r.frame_id].add(r.pk)
                    class_distribution[lname] += 1
    return class_distribution, class_names, rboxes, rboxes_set, frames, i_class_names


def download_s3_dir(client, resource, dist, local, bucket):
    """
    Taken from http://stackoverflow.com/questions/31918960/boto3-to-download-all-files-from-a-s3-bucket
    :param client:
    :param resource:
    :param dist:
    :param local:
    :param bucket:
    :return:
    """
    paginator = client.get_paginator('list_objects')
    for result in paginator.paginate(Bucket=bucket, Delimiter='/', Prefix=dist, RequestPayer='requester'):
        if result.get('CommonPrefixes') is not None:
            for subdir in result.get('CommonPrefixes'):
                download_s3_dir(client, resource, subdir.get('Prefix'), local, bucket)
        if result.get('Contents') is not None:
            for ffile in result.get('Contents'):
                if not os.path.exists(os.path.dirname(local + os.sep + ffile.get('Key'))):
                    os.makedirs(os.path.dirname(local + os.sep + ffile.get('Key')))
                resource.meta.client.download_file(bucket, ffile.get('Key'), local + os.sep + ffile.get('Key'),
                                                   ExtraArgs={'RequestPayer': 'requester'})

def perform_export(s3_export):
    s3 = boto3.resource('s3')
    s3bucket = s3_export.arguments['bucket']
    s3region = s3_export.arguments['region']
    s3key = s3_export.arguments['key']
    if s3region == 'us-east-1':
        s3.create_bucket(Bucket=s3bucket)
    else:
        s3.create_bucket(Bucket=s3bucket, CreateBucketConfiguration={'LocationConstraint': s3region})
    time.sleep(20)  # wait for it to create the bucket
    path = "{}/{}/".format(settings.MEDIA_ROOT, s3_export.video.pk)
    video = s3_export.video
    a = serializers.VideoExportSerializer(instance=video)
    data = copy.deepcopy(a.data)
    data['labels'] = serializers.serialize_video_labels(video)
    data['export_event_pk'] = s3_export.pk
    exists = False
    try:
        s3.Object(s3bucket, '{}/table_data.json'.format(s3key).replace('//', '/')).load()
    except ClientError as e:
        if e.response['Error']['Code'] != "404":
            raise ValueError,"Key s3://{}/{}/table_data.json already exists".format(s3bucket,s3key)
    else:
        return -1, "Error key already exists"
    with file("{}/{}/table_data.json".format(settings.MEDIA_ROOT, s3_export.video.pk), 'w') as output:
        json.dump(data, output)
    s3bucket = s3_export.arguments['bucket']
    s3key = s3_export.arguments['key']
    upload = subprocess.Popen(args=["aws", "s3", "sync",'--quiet', ".", "s3://{}/{}/".format(s3bucket,s3key)],cwd=path)
    upload.communicate()
    upload.wait()
    s3_export.completed = True
    s3_export.save()
    return upload.returncode, ""


def perform_substitution(args,parent_task,inject_filters):
    """
    Its important to do a deep copy of args before executing any mutations.
    :param args:
    :param parent_task:
    :return:
    """
    args = copy.deepcopy(args) # IMPORTANT otherwise the first task to execute on the worker will fill the filters
    inject_filters = copy.deepcopy(inject_filters) # IMPORTANT otherwise the first task to execute on the worker will fill the filters
    filters = args.get('filters',{})
    parent_args = parent_task.arguments
    if filters == '__parent__':
        parent_filters = parent_args.get('filters',{})
        logging.info('using filters from parent arguments: {}'.format(parent_args))
        args['filters'] = parent_filters
    elif filters:
        for k,v in args.get('filters',{}).items():
            if v == '__parent_event__':
                args['filters'][k] = parent_task.pk
            elif v == '__grand_parent_event__':
                args['filters'][k] = parent_task.parent.pk
    if inject_filters:
        if 'filters' not in args:
            args['filters'] = inject_filters
        else:
            args['filters'].update(inject_filters)
    return args


def process_next(task_id,inject_filters=None,custom_next_tasks=None,sync=True,launch_next=True):
    if custom_next_tasks is None:
        custom_next_tasks = []
    dt = TEvent.objects.get(pk=task_id)
    launched = []
    logging.info("next tasks for {}".format(dt.operation))
    next_tasks = dt.arguments.get('next_tasks',[]) if dt.arguments and launch_next else []
    if sync:
        for k in settings.SYNC_TASKS.get(dt.operation,[]):
            args = perform_substitution(k['arguments'], dt,inject_filters)
            logging.info("launching {}, {} with args {} as specified in config".format(dt.operation, k['operation'], args))
            next_task = TEvent.objects.create(video=dt.video,operation=k['operation'],arguments=args,
                                              parent=dt,parent_process=dt.parent_process)
            launched.append(app.send_task(k['operation'], args=[next_task.pk, ],
                                          queue=get_queue_name(k['operation'],args)).id)
    for k in next_tasks+custom_next_tasks:
        args = perform_substitution(k['arguments'], dt,inject_filters)
        logging.info("launching {}, {} with args {} as specified in next_tasks".format(dt.operation, k['operation'], args))
        next_task = TEvent.objects.create(video=dt.video,operation=k['operation'], arguments=args,
                                          parent=dt,parent_process=dt.parent_process)
        launched.append(app.send_task(k['operation'], args=[next_task.pk, ],
                                      queue=get_queue_name(k['operation'],args)).id)
    return launched


def celery_40_bug_hack(start):
    """
    Celery 4.0.2 retries tasks due to ACK issues when running in solo mode,
    Since Tensorflow ncessiates use of solo mode, we can manually check if the task is has already run and quickly finis it
    Since the code never uses Celery results except for querying and retries are handled at application level this solves the
    issue
    :param start:
    :return:
    """
    return start.started
