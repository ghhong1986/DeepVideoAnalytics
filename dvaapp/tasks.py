from __future__ import absolute_import
import subprocess, shutil, os, time, logging, copy, calendar, requests, json, zipfile, random, io, boto3
from collections import defaultdict
from PIL import Image
from django.conf import settings
from dva.celery import app
from .models import Video, Frame, TEvent,  IndexEntries, ClusterCodes, Region, Tube, Clusters, CustomDetector, Segment, IndexerQuery
from .operations.indexing import IndexerTask
from .operations.retrieval import RetrieverTask
from .operations.detection import DetectorTask
from .operations.segmentation import SegmentorTask
from .operations.analysis import AnalyzerTask
from .operations.decoding import VideoDecoder
from dvalib import clustering
from datetime import datetime
from . import shared
try:
    import numpy as np
except ImportError:
    pass
from . import serializers


@app.task(track_started=True, name="perform_indexing", base=IndexerTask)
def perform_indexing(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_indexing.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_indexing.name
    json_args = start.arguments
    target = json_args.get('target','frames')
    index_name = json_args['index']
    start.save()
    start_time = time.time()
    visual_index = perform_indexing.visual_indexer[index_name]
    sync = True
    if target == 'query':
        iq = IndexerQuery.objects.get(id=json_args['iq_id'])
        local_path = "{}/queries/{}_{}.png".format(settings.MEDIA_ROOT, iq.algorithm, iq.parent_query.pk)
        with open(local_path, 'w') as fh:
            fh.write(str(iq.parent_query.image_data))
        vector = visual_index.apply(local_path)
        # TODO: figure out a better way to store numpy arrays.
        s = io.BytesIO()
        np.save(s,vector)
        iq.vector = s.getvalue()
        iq.save()
        iq.parent_query.results_available = True
        iq.parent_query.save()
        sync = False
    else:
        video_id = start.video_id
        dv = Video.objects.get(id=video_id)
        arguments = json_args.get('filters', {})
        arguments['video_id'] = dv.pk
        media_dir = settings.MEDIA_ROOT
        if target == 'frames':
            frames = Frame.objects.all().filter(**arguments)
            index_name, index_results, feat_fname, entries_fname = perform_indexing.index_frames(media_dir,frames, visual_index,start.pk)
            detection_name = 'Frames_subset_by_{}'.format(start.pk)
            contains_frames = True
            contains_detections = False
        elif target == 'regions':
            detections = Region.objects.all().filter(**arguments)
            logging.info("Indexing {} Regions".format(detections.count()))
            detection_name = 'Faces_subset_by_{}'.format(start.pk) if index_name == 'facenet' else 'Regions_subset_by_{}'.format(start.pk)
            index_name, index_results, feat_fname, entries_fname = perform_indexing.index_regions(media_dir, detections, detection_name, visual_index)
            contains_frames = False
            contains_detections = True
        else:
            raise NotImplementedError
        if entries_fname:
            i = IndexEntries()
            i.video = dv
            i.count = len(index_results)
            i.contains_detections = contains_detections
            i.contains_frames = contains_frames
            i.detection_name = detection_name
            i.algorithm = index_name
            i.entries_file_name = entries_fname.split('/')[-1]
            i.features_file_name = feat_fname.split('/')[-1]
            i.source = start
            i.source_filter_json = arguments
            i.save()
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return shared.process_next(task_id,sync=sync)


@app.task(track_started=True, name="perform_transformation")
def perform_transformation(task_id):
    """
    Crop detected or annotated regions
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_transformation.request.id
    start.started = True
    start.ts = datetime.now()
    start_time = time.time()
    start.operation = perform_transformation.name
    video_id = start.video_id
    args = start.arguments
    resize = args.get('resize',None)
    kwargs = args.get('filters',{})
    paths_to_regions = defaultdict(list)
    kwargs['video_id'] = start.video_id
    kwargs['materialized'] = False
    logging.info("executing crop with kwargs {}".format(kwargs))
    queryset = Region.objects.all().filter(**kwargs)
    for dr in queryset:
        path = "{}/{}/frames/{}.jpg".format(settings.MEDIA_ROOT,video_id,dr.parent_frame_index)
        paths_to_regions[path].append(dr)
    for path,regions in paths_to_regions.iteritems():
        img = Image.open(path)
        for dr in regions:
            cropped = img.crop((dr.x, dr.y,dr.x + dr.w, dr.y + dr.h))
            if resize:
                resized = cropped.resize(tuple(resize),Image.BICUBIC)
                resized.save("{}/{}/regions/{}.jpg".format(settings.MEDIA_ROOT, video_id, dr.id))
            else:
                cropped.save("{}/{}/regions/{}.jpg".format(settings.MEDIA_ROOT, video_id, dr.id))
    queryset.update(materialized=True)
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="perform_retrieval", base=RetrieverTask)
def perform_retrieval(task_id):
    start = TEvent.objects.get(pk=task_id)
    start_time = time.time()
    if shared.celery_40_bug_hack(start):
        return 0
    args = start.arguments
    start.task_id = perform_retrieval.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_retrieval.name
    start.save()
    iq = IndexerQuery.objects.get(pk=args['iq_id'])
    perform_retrieval.retrieve(iq,iq.algorithm)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True, name="extract_frames")
def extract_frames(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = extract_frames.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = extract_frames.name
    args = start.arguments
    if args == {}:
        args['rescale'] = 0
        args['rate'] = 30
        start.arguments = args
    start.save()
    start_time = time.time()
    video_id = start.video_id
    dv = Video.objects.get(id=video_id)
    if dv.youtube_video:
        shared.create_video_folders(dv)
    v = VideoDecoder(dvideo=dv, media_dir=settings.MEDIA_ROOT)
    v.extract(args=args,start=start)
    if args.get('sync',False):
        # No need to inject just process everything together
        shared.process_next(task_id)
    else:
        step = args.get("frames_batch_size",settings.DEFAULT_FRAMES_BATCH_SIZE)
        for gte, lt in [(k, k + step) for k in range(0, dv.frames, step)]:
            if lt < dv.frames: # to avoid off by one error
                filters = {'frame_index__gte': gte, 'frame_index__lt': lt}
            else:
                filters = {'frame_index__gte': gte}
            shared.process_next(task_id,inject_filters=filters)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    os.remove("{}/{}/video/{}.zip".format(settings.MEDIA_ROOT, dv.pk, dv.pk))
    return 0


@app.task(track_started=True, name="segment_video")
def segment_video(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = segment_video.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = segment_video.name
    args = start.arguments
    if 'rescale' not in args:
        args['rescale'] = 0
    if 'rate' not in args:
        args['rate'] = 30
    start.arguments = args
    start.save()
    start_time = time.time()
    video_id = start.video_id
    dv = Video.objects.get(id=video_id)
    if dv.youtube_video:
        shared.create_video_folders(dv)
    v = VideoDecoder(dvideo=dv, media_dir=settings.MEDIA_ROOT)
    v.get_metadata()
    v.segment_video()
    if args.get('sync',False):
        next_args = {'rescale': args['rescale'], 'rate': args['rate']}
        next_task = TEvent.objects.create(video=dv, operation='decode_video', arguments=next_args, parent=start)
        decode_video(next_task.pk)  # decode it synchronously for testing in Travis
        shared.process_next(task_id)
    else:
        step = args.get("segments_batch_size",settings.DEFAULT_SEGMENTS_BATCH_SIZE)
        for gte, lt in [(k, k + step) for k in range(0, dv.segments, step)]:
            if lt < dv.segments:
                filters = {'segment_index__gte': gte, 'segment_index__lt': lt}
            else:
                # ensures off by one error does not happens [gte->
                filters = {'segment_index__gte': gte}
            shared.process_next(task_id, inject_filters=filters,sync=False) # Dont sync multiple times
        shared.process_next(task_id,launch_next=False) # Sync
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True,name="decode_video",ignore_result=False)
def decode_video(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = decode_video.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = decode_video.name
    args = start.arguments
    start.save()
    start_time = time.time()
    video_id = start.video_id
    dv = Video.objects.get(id=video_id)
    kwargs = args.get('filters',{})
    kwargs['video_id'] = video_id
    v = VideoDecoder(dvideo=dv, media_dir=settings.MEDIA_ROOT)
    for ds in Segment.objects.filter(**kwargs):
        v.decode_segment(ds=ds,denominator=args['rate'],rescale=args['rescale'])
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return task_id


@app.task(track_started=True, name="perform_detection",base=DetectorTask)
def perform_detection(task_id):
    """
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_detection.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_detection.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    args = start.arguments
    detector_name = args['detector']
    detector = perform_detection.get_static_detectors[detector_name]
    if detector.session is None:
        logging.info("loading detection model")
        detector.load()
    dv = Video.objects.get(id=video_id)
    if 'filters' in args:
        kwargs = args['filters']
        kwargs['video_id'] = video_id
        frames = Frame.objects.all().filter(**kwargs)
        logging.info("Running {} Using filters {}".format(detector_name,kwargs))
    else:
        frames = Frame.objects.all().filter(video=dv)
    dd_list = []
    path_list = []
    for df in frames:
        local_path = "{}/{}/frames/{}.jpg".format(settings.MEDIA_ROOT,video_id,df.frame_index)
        detections = detector.detect(local_path)
        for d in detections:
            dd = Region()
            dd.region_type = Region.DETECTION
            dd.video_id = dv.pk
            dd.frame_id = df.pk
            dd.parent_frame_index = df.frame_index
            dd.parent_segment_index = df.segment_index
            if detector_name == 'coco':
                dd.object_name = 'SSD_{}'.format(d['object_name'])
                dd.confidence = 100.0 * d['score']
            elif detector_name == 'textbox':
                dd.object_name = 'TEXTBOX'
                dd.confidence = 100.0 * d['score']
            elif detector_name == 'face':
                dd.object_name = 'MTCNN_face'
                dd.confidence = 100.0
            else:
                raise NotImplementedError
            dd.x = d['x']
            dd.y = d['y']
            dd.w = d['w']
            dd.h = d['h']
            dd.event_id = task_id
            dd_list.append(dd)
            path_list.append(local_path)
    _ = Region.objects.bulk_create(dd_list,1000)
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True, name="perform_analysis",base_task=AnalyzerTask)
def perform_analysis(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_analysis.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_analysis.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    args = start.arguments
    target = args['target']
    analyzer_name = args['analyzer']
    analyzer = perform_analysis.get_static_analyzers[analyzer_name]
    kwargs = args.get('filters',{})
    kwargs['video_id'] = video_id
    regions_batch = []
    if target == 'frames':
        queryset = Frame.objects.all().filter(**kwargs)
        for f in queryset:
            path = '{}/{}/frames/{}.jpg'.format(settings.MEDIA_ROOT,video_id,f.frame_index)
            tags = analyzer.apply(path)
            a = Region()
            a.object_name = "OpenImagesTag"
            a.text = " ".join([t for t,v in tags.iteritems() if v > 0.1])
            a.metadata = {t:100.0*v for t,v in tags.iteritems() if v > 0.1}
            a.frame_id = f.id
            a.full_frame = True
            regions_batch.append(a)
    elif target == 'regions':
        queryset = Region.objects.all().filter(**kwargs)
        raise NotImplementedError
    else:
        raise NotImplementedError
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True, name="export_video")
def export_video(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = export_video.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = export_video.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    video_obj = Video.objects.get(pk=video_id)
    file_name = '{}_{}.dva_export.zip'.format(video_id, int(calendar.timegm(time.gmtime())))
    args = {'file_name':file_name}
    start.arguments = args
    try:
        os.mkdir("{}/{}".format(settings.MEDIA_ROOT, 'exports'))
    except:
        pass
    outdirname = "{}/exports/{}".format(settings.MEDIA_ROOT, video_id)
    if os.path.isdir(outdirname):
        shutil.rmtree(outdirname)
    shutil.copytree('{}/{}'.format(settings.MEDIA_ROOT, video_id),
                    "{}/exports/{}".format(settings.MEDIA_ROOT, video_id))
    a = serializers.VideoExportSerializer(instance=video_obj)
    data = copy.deepcopy(a.data)
    data['labels'] = serializers.serialize_video_labels(video_obj)
    data['export_event_pk'] = start.pk
    with file("{}/exports/{}/table_data.json".format(settings.MEDIA_ROOT, video_id), 'w') as output:
        json.dump(data, output)
    zipper = subprocess.Popen(['zip', file_name, '-r', '{}'.format(video_id)],
                              cwd='{}/exports/'.format(settings.MEDIA_ROOT))
    zipper.wait()
    if zipper.returncode != 0:
        start.errored = True
        start.error_message = "Could not zip {}".format(zipper.returncode)
        start.seconds = time.time() - start_time
        start.save()
        raise ValueError, start.error_message
    shutil.rmtree("{}/exports/{}".format(settings.MEDIA_ROOT, video_id))
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return file_name


@app.task(track_started=True, name="import_video")
def import_video(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = import_video.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = import_video.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    video_obj = Video.objects.get(pk=video_id)
    zipf = zipfile.ZipFile("{}/{}/{}.zip".format(settings.MEDIA_ROOT, video_id, video_id), 'r')
    zipf.extractall("{}/{}/".format(settings.MEDIA_ROOT, video_id))
    zipf.close()
    video_root_dir = "{}/{}/".format(settings.MEDIA_ROOT, video_id)
    old_key = None
    for k in os.listdir(video_root_dir):
        unzipped_dir = "{}{}".format(video_root_dir, k)
        if os.path.isdir(unzipped_dir):
            for subdir in os.listdir(unzipped_dir):
                shutil.move("{}/{}".format(unzipped_dir, subdir), "{}".format(video_root_dir))
            shutil.rmtree(unzipped_dir)
            break
    with open("{}/{}/table_data.json".format(settings.MEDIA_ROOT, video_id)) as input_json:
        video_json = json.load(input_json)
    importer = serializers.VideoImporter(video=video_obj,json=video_json,root_dir=video_root_dir)
    importer.import_video()
    source_zip = "{}/{}.zip".format(video_root_dir, video_obj.pk)
    os.remove(source_zip)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="import_vdn_file")
def import_vdn_file(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.started = True
    start.ts = datetime.now()
    start.task_id = import_vdn_file.request.id
    start.operation = import_vdn_file.name
    start.save()
    start_time = time.time()
    dv = start.video
    shared.create_video_folders(dv, create_subdirs=False)
    if 'www.dropbox.com' in dv.vdn_dataset.download_url and not dv.vdn_dataset.download_url.endswith('?dl=1'):
        r = requests.get(dv.vdn_dataset.download_url + '?dl=1')
    else:
        r = requests.get(dv.vdn_dataset.download_url)
    output_filename = "{}/{}/{}.zip".format(settings.MEDIA_ROOT, dv.pk, dv.pk)
    with open(output_filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)
    r.close()
    zipf = zipfile.ZipFile("{}/{}/{}.zip".format(settings.MEDIA_ROOT, dv.pk, dv.pk), 'r')
    zipf.extractall("{}/{}/".format(settings.MEDIA_ROOT, dv.pk))
    zipf.close()
    video_root_dir = "{}/{}/".format(settings.MEDIA_ROOT, dv.pk)
    for k in os.listdir(video_root_dir):
        unzipped_dir = "{}{}".format(video_root_dir, k)
        if os.path.isdir(unzipped_dir):
            for subdir in os.listdir(unzipped_dir):
                shutil.move("{}/{}".format(unzipped_dir, subdir), "{}".format(video_root_dir))
            shutil.rmtree(unzipped_dir)
            break
    with open("{}/{}/table_data.json".format(settings.MEDIA_ROOT, dv.pk)) as input_json:
        video_json = json.load(input_json)
    importer = serializers.VideoImporter(video=dv,json=video_json,root_dir=video_root_dir)
    importer.import_video()
    source_zip = "{}/{}.zip".format(video_root_dir, dv.pk)
    os.remove(source_zip)
    dv.uploaded = True
    dv.save()
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="import_vdn_detector_file")
def import_vdn_detector_file(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.started = True
    start.ts = datetime.now()
    start.task_id = import_vdn_detector_file.request.id
    start.operation = import_vdn_detector_file.name
    start.save()
    start_time = time.time()
    dd = CustomDetector.objects.get(pk=start.arguments['detector_pk'])
    shared.create_detector_folders(dd, create_subdirs=False)
    if 'www.dropbox.com' in dd.vdn_detector.download_url and not dd.vdn_detector.download_url.endswith('?dl=1'):
        r = requests.get(dd.vdn_detector.download_url + '?dl=1')
    else:
        r = requests.get(dd.vdn_detector.download_url)
    output_filename = "{}/detectors/{}.zip".format(settings.MEDIA_ROOT, dd.pk)
    with open(output_filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)
    r.close()
    source_zip = "{}/detectors/{}.zip".format(settings.MEDIA_ROOT, dd.pk)
    zipf = zipfile.ZipFile(source_zip, 'r')
    zipf.extractall("{}/detectors/{}/".format(settings.MEDIA_ROOT, dd.pk))
    zipf.close()
    serializers.import_detector(dd)
    dd.save()
    os.remove(source_zip)
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="import_vdn_s3")
def import_vdn_s3(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.started = True
    start.ts = datetime.now()
    start.task_id = import_vdn_s3.request.id
    start.operation = import_vdn_s3.name
    start.save()
    start_time = time.time()
    dv = start.video
    shared.create_video_folders(dv, create_subdirs=False)
    client = boto3.client('s3')
    resource = boto3.resource('s3')
    key = dv.vdn_dataset.aws_key
    bucket = dv.vdn_dataset.aws_bucket
    if key.endswith('.dva_export.zip'):
        ofname = "{}/{}/{}.zip".format(settings.MEDIA_ROOT, dv.pk, dv.pk)
        resource.meta.client.download_file(bucket, key, ofname,ExtraArgs={'RequestPayer': 'requester'})
        zipf = zipfile.ZipFile(ofname, 'r')
        zipf.extractall("{}/{}/".format(settings.MEDIA_ROOT, dv.pk))
        zipf.close()
        video_root_dir = "{}/{}/".format(settings.MEDIA_ROOT, dv.pk)
        for k in os.listdir(video_root_dir):
            unzipped_dir = "{}{}".format(video_root_dir, k)
            if os.path.isdir(unzipped_dir):
                for subdir in os.listdir(unzipped_dir):
                    shutil.move("{}/{}".format(unzipped_dir, subdir), "{}".format(video_root_dir))
                shutil.rmtree(unzipped_dir)
                break
        source_zip = "{}/{}.zip".format(video_root_dir, dv.pk)
        os.remove(source_zip)
    else:
        video_root_dir = "{}/{}/".format(settings.MEDIA_ROOT, dv.pk)
        path = "{}/{}/".format(settings.MEDIA_ROOT, dv.pk)
        shared.download_s3_dir(client, resource, key, path, bucket)
        for filename in os.listdir(os.path.join(path, key)):
            shutil.move(os.path.join(path, key, filename), os.path.join(path, filename))
        os.rmdir(os.path.join(path, key))
    with open("{}/{}/table_data.json".format(settings.MEDIA_ROOT, dv.pk)) as input_json:
        video_json = json.load(input_json)
    importer = serializers.VideoImporter(video=dv,json=video_json,root_dir=video_root_dir)
    importer.import_video()
    dv.uploaded = True
    dv.save()
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()




@app.task(track_started=True, name="backup_video_to_s3")
def backup_video_to_s3(s3_export_id):
    start = TEvent.objects.get(pk=s3_export_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.started = True
    start.ts = datetime.now()
    start.task_id = backup_video_to_s3.request.id
    start.operation = backup_video_to_s3.name
    start.save()
    start_time = time.time()
    returncode, errormsg = shared.perform_export(start)
    if returncode == 0:
        start.completed = True
    else:
        start.errored = True
        start.error_message = errormsg
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="push_video_to_vdn_s3")
def push_video_to_vdn_s3(s3_export_id):
    start = TEvent.objects.get(pk=s3_export_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = push_video_to_vdn_s3.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = push_video_to_vdn_s3.name
    start.save()
    start_time = time.time()
    returncode, errormsg = shared.perform_export(start)
    if returncode == 0:
        start.completed = True
    else:
        start.errored = True
        start.error_message = errormsg
    start.seconds = time.time() - start_time
    start.save()




@app.task(track_started=True, name="import_video_from_s3")
def import_video_from_s3(s3_import_id):
    start = TEvent.objects.get(pk=s3_import_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.started = True
    start.ts = datetime.now()
    start.task_id = import_video_from_s3.request.id
    start.operation = import_video_from_s3.name
    start.save()
    start_time = time.time()
    s3key = start.arguments['key']
    s3bucket = start.arguments['bucket']
    logging.info("processing key  {}space".format(s3key))
    if start.video is None:
        v = Video()
        v.name = "pending S3 import from s3://{}/{}".format(s3bucket,s3key)
        v.save()
        start.video = v
        start.save()
    path = "{}/{}/".format(settings.MEDIA_ROOT, start.video.pk)
    if s3key.strip() and (s3key.endswith('.zip') or s3key.endswith('.mp4')):
        fname = 'temp_' + str(time.time()).replace('.', '_') + '_' + str(random.randint(0, 100)) + '.' + \
                s3key.split('.')[-1]
        command = ["aws", "s3", "cp",'--quiet', "s3://{}/{}".format(s3bucket, s3key), fname]
        path = "{}/".format(settings.MEDIA_ROOT)
        download = subprocess.Popen(args=command, cwd=path)
        download.communicate()
        download.wait()
        if download.returncode != 0:
            start.errored = True
            start.error_message = "return code for '{}' was {}".format(" ".join(command), download.returncode)
            start.seconds = time.time() - start_time
            start.save()
            raise ValueError, start.error_message
        shared.handle_downloaded_file("{}/{}".format(settings.MEDIA_ROOT, fname), start.video, "s3://{}/{}".format(s3bucket,s3key))
        start.completed = True
        start.seconds = time.time() - start_time
        start.save()
    else:
        shared.create_video_folders(start.video, create_subdirs=False)
        command = ["aws", "s3", "cp",'--quiet', "s3://{}/{}/".format(s3bucket, s3key), '.', '--recursive']
        command_exec = " ".join(command)
        download = subprocess.Popen(args=command, cwd=path)
        download.communicate()
        download.wait()
        if download.returncode != 0:
            start.errored = True
            start.error_message = "return code for '{}' was {}".format(command_exec, download.returncode)
            start.seconds = time.time() - start_time
            start.save()
            raise ValueError, start.error_message
        with open("{}/{}/table_data.json".format(settings.MEDIA_ROOT, start.video.pk)) as input_json:
            video_json = json.load(input_json)
        importer = serializers.VideoImporter(video=start.video, json=video_json, root_dir=path)
        importer.import_video()
    shared.process_next(start.pk)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="perform_clustering")
def perform_clustering(cluster_task_id, test=False):
    start = TEvent.objects.get(pk=cluster_task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_clustering.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_clustering.name
    start.save()
    start_time = time.time()
    clusters_dir = "{}/clusters/".format(settings.MEDIA_ROOT)
    if not os.path.isdir(clusters_dir):
        os.mkdir(clusters_dir)
    dc = Clusters.objects.get(pk=start.arguments['clusters_id'])
    fnames = []
    for ipk in dc.included_index_entries_pk:
        k = IndexEntries.objects.get(pk=ipk)
        fnames.append("{}/{}/indexes/{}".format(settings.MEDIA_ROOT, k.video.pk, k.features_file_name))
    cluster_proto_filename = "{}{}.proto".format(clusters_dir, dc.pk)
    c = clustering.Clustering(fnames, dc.components, cluster_proto_filename, m=dc.m, v=dc.v, sub=dc.sub, test_mode=test)
    c.cluster()
    cluster_codes = []
    for e in c.entries:
        cc = ClusterCodes()
        cc.video_id = e['video_primary_key']
        if 'detection_primary_key' in e:
            cc.detection_id = e['detection_primary_key']
            cc.frame_id = Region.objects.get(pk=cc.detection_id).frame_id
        else:
            cc.frame_id = e['frame_primary_key']
        cc.clusters = dc
        cc.coarse = e['coarse']
        cc.fine = e['fine']
        cc.coarse_text = " ".join(map(str, e['coarse']))
        cc.fine_text = " ".join(map(str, e['fine']))
        cc.searcher_index = e['index']
        cluster_codes.append(cc)
    ClusterCodes.objects.bulk_create(cluster_codes)
    c.save()
    dc.completed = True
    dc.save()
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()


@app.task(track_started=True, name="sync_bucket")
def sync_bucket(task_id):
    """
    TODO: Determine a way to rate limit consecutive sync bucket for a given
    (video,directory). As an alternative perform sync at a more granular level,
    e.g. individual files. This is most important when syncing regions and frames.
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = sync_bucket.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = sync_bucket.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    args = start.arguments
    if settings.MEDIA_BUCKET.strip():
        if 'dirname' in args:
            src = '{}/{}/{}/'.format(settings.MEDIA_ROOT, video_id, args['dirname'])
            dest = 's3://{}/{}/{}/'.format(settings.MEDIA_BUCKET, video_id, args['dirname'])
        else:
            src = '{}/{}/'.format(settings.MEDIA_ROOT, video_id)
            dest = 's3://{}/{}/'.format(settings.MEDIA_BUCKET, video_id)
        command = " ".join(['aws', 's3', 'sync','--quiet', src, dest])
        syncer = subprocess.Popen(['aws', 's3', 'sync','--quiet', '--size-only', src, dest])
        syncer.wait()
        if syncer.returncode != 0:
            start.errored = True
            start.error_message = "Error while executing : {}".format(command)
            start.save()
            return
    else:
        logging.info("Media bucket name not specified, nothing was synced.")
        start.error_message = "Media bucket name is empty".format(settings.MEDIA_BUCKET)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return


@app.task(track_started=True, name="delete_video_by_id")
def delete_video_by_id(task_id):
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = delete_video_by_id.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = delete_video_by_id.name
    start.save()
    start_time = time.time()
    args = start.arguments
    video_id = int(args['video_pk'])
    src = '{}/{}/'.format(settings.MEDIA_ROOT, int(video_id))
    args = ['rm','-rf',src]
    command = " ".join(args)
    deleter = subprocess.Popen(args)
    deleter.wait()
    if deleter.returncode != 0:
        start.errored = True
        start.error_message = "Error while executing : {}".format(command)
        start.save()
        return
    if settings.MEDIA_BUCKET.strip():
        dest = 's3://{}/{}/'.format(settings.MEDIA_BUCKET, int(video_id))
        args = ['aws', 's3', 'rm','--quiet','--recursive', dest]
        command = " ".join(args)
        syncer = subprocess.Popen(args)
        syncer.wait()
        if syncer.returncode != 0:
            start.errored = True
            start.error_message = "Error while executing : {}".format(command)
            start.save()
            return
    else:
        logging.info("Media bucket name not specified, nothing was synced.")
        start.error_message = "Media bucket name is empty".format(settings.MEDIA_BUCKET)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return


@app.task(track_started=True, name="detect_custom_objects")
def detect_custom_objects(task_id):
    """
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = detect_custom_objects.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = detect_custom_objects.name
    start.save()
    start_time = time.time()
    args = start.arguments
    video_id = start.video_id
    detector_id = args['detector_pk']
    custom_detector = subprocess.Popen(['fab', 'detect_custom_objects:{},{}'.format(detector_id,video_id)],cwd=os.path.join(os.path.abspath(__file__).split('tasks.py')[0], '../'))
    custom_detector.wait()
    if custom_detector.returncode != 0:
        start.errored = True
        start.error_message = "fab detect_custom_objects failed with return code {}".format(custom_detector.returncode)
        start.seconds = time.time() - start_time
        start.save()
        raise ValueError, start.error_message
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True, name="train_yolo_detector")
def train_yolo_detector(task_id):
    """
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = train_yolo_detector.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = train_yolo_detector.name
    start.save()
    start_time = time.time()
    train_detector = subprocess.Popen(['fab', 'train_yolo:{}'.format(start.pk)],cwd=os.path.join(os.path.abspath(__file__).split('tasks.py')[0], '../'))
    train_detector.wait()
    if train_detector.returncode != 0:
        start.errored = True
        start.error_message = "fab train_yolo:{} failed with return code {}".format(start.pk,train_detector.returncode)
        start.seconds = time.time() - start_time
        start.save()
        raise ValueError, start.error_message
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0


@app.task(track_started=True,name="update_index")
def update_index(indexer_entry_pk):
    """
    app.send_task('update_index',args=[5,],exchange='broadcast_tasks')
    :param indexer_entry_pk:
    :return:
    """
    print "TESTSTESTSTSTSTSTST"
    logging.info("recieved {}".format(indexer_entry_pk))
    return 0


@app.task(track_started=True, name="perform_segmentation",base=SegmentorTask)
def perform_segmentation(task_id):
    """
    :param task_id:
    :return:
    """
    start = TEvent.objects.get(pk=task_id)
    if shared.celery_40_bug_hack(start):
        return 0
    start.task_id = perform_segmentation.request.id
    start.started = True
    start.ts = datetime.now()
    start.operation = perform_segmentation.name
    start.save()
    start_time = time.time()
    video_id = start.video_id
    args = start.arguments
    segmentor_name = args['segmentor']
    segmentor = perform_segmentation.get_static_segmentors[segmentor_name]
    if segmentor.session is None:
        logging.info("loading detection model")
        segmentor.load()
    dv = Video.objects.get(id=video_id)
    if 'filters' in args:
        kwargs = args['filters']
        kwargs['video_id'] = video_id
        frames = Frame.objects.all().filter(**kwargs)
        logging.info("Running {} Using filters {}".format(segmentor_name,kwargs))
    else:
        frames = Frame.objects.all().filter(video=dv)
    dd_list = []
    path_list = []
    for df in frames:
        local_path = "{}/{}/frames/{}.jpg".format(settings.MEDIA_ROOT,video_id,df.frame_index)
        segmentation = segmentor.detect(local_path)
        # for d in detections:
        #     dd = Region()
        #     dd.region_type = Region.DETECTION
        #     dd.video_id = dv.pk
        #     dd.frame_id = df.pk
        #     dd.parent_frame_index = df.frame_index
        #     dd.parent_segment_index = df.segment_index
        #     if detector_name == 'coco':
        #         dd.object_name = 'SSD_{}'.format(d['object_name'])
        #         dd.confidence = 100.0 * d['score']
        #     elif detector_name == 'textbox':
        #         dd.object_name = 'TEXTBOX'
        #         dd.confidence = 100.0 * d['score']
        #     elif detector_name == 'face':
        #         dd.object_name = 'MTCNN_face'
        #         dd.confidence = 100.0
        #     else:
        #         raise NotImplementedError
        #     dd.x = d['x']
        #     dd.y = d['y']
        #     dd.w = d['w']
        #     dd.h = d['h']
        #     dd.event_id = task_id
        #     dd_list.append(dd)
        #     path_list.append(local_path)
    _ = Region.objects.bulk_create(dd_list,1000)
    shared.process_next(task_id)
    start.completed = True
    start.seconds = time.time() - start_time
    start.save()
    return 0
