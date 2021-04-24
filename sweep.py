import os, sys, logging, argparse, glob, time, subprocess, json, telegram
from datetime import datetime
from tempfile import TemporaryDirectory

def post_video(args, clip_path, caption):

    logging.info("Posting %s to Telegram '%s'", clip_path, caption)
    if args.dummy_run:
        return
    try:
        with open(clip_path, 'rb') as video:
            m = args.bot.send_video(chat_id=args.chat_id,
                                    video=video,
                                    caption=caption,timeout=60,disable_notification=True)
    except Exception as e:
        logging.exception(e)

def post_image(args, image_path, caption):

    logging.info("Posting %s to Telegram '%s'", image_path, caption)
    if args.dummy_run:
        return
    try:
        with open(image_path, 'rb') as photo:
            m = args.bot.send_photo(chat_id=args.chat_id,
                                    photo=photo,
                                    caption=caption,timeout=60,disable_notification=True)
    except Exception as e:
        logging.exception(e)

def find_new_clips(args):

    """Find new files that have not been renamed in previous processing.
       Filter and sort by age.
    """
    now = time.time()
    clips = [c for c in glob.iglob(f'{args.root}/**/*.mp4', recursive=True) if '@' not in c]
    clips_to_process = []
    for clip in clips:
        attrs = os.stat(clip)
        age_in_seconds = now - attrs.st_mtime
        if age_in_seconds > args.max_age_seconds:
            logging.warning("%s is too old to process (%.2f seconds old)", clip, age_in_seconds)
            continue
        if age_in_seconds < 30:
            logging.warning("%s is too young to process (%.2f seconds old)", clip, age_in_seconds)
            continue
        clips_to_process.append({'path': clip, 'age_in_seconds': age_in_seconds,
                                 'mtime': attrs.st_mtime,
                                 'Mbytes': attrs.st_size/2**20})
    clips_to_process = sorted(clips_to_process, key=lambda x: x['age_in_seconds'], reverse=True)
    return clips_to_process

def downscale(args, clip_path, clip_name, tmpdir):

    current_mbytes = os.stat(clip_path).st_size/2**20
    if not args.downscale or current_mbytes <= args.max_telegram_mbytes:
        return clip_path, current_mbytes, None
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=width,height',
                                '-of', 'json', clip_path],
                                stdout=subprocess.PIPE).stdout.decode('utf-8')
    except Exception as e:
        logging.exception(e)
        return clip_path, current_mbytes, None

    if result.returncode != 0:
        logging.error(result.stderr.decode("utf-8"))
        return clip_path, current_mbytes, None

    width = json.loads(result)['streams'][0]['width']
    height = json.loads(result)['streams'][0]['height']
    current_scale = "%d:%d" % (width, height)
    logging.info("%.2fMb > %dMb so trying to downscale", current_mbytes, args.max_telegram_mbytes)
    logging.debug("%s scale is currently %d:%d", clip_name, width, height)
    aspect_ratio = round(width/height,2)
    same_aspect = [r for r in args.resolutions if round(int(r.split(":")[0])/int(r.split(":")[1]),2) == aspect_ratio]
    pixels = width * height
    resolutions = [r for r in same_aspect if int(r.split(":")[0])*int(r.split(":")[1]) < pixels]   
    if len(resolutions) == 0:
        logging.warning("No lower resolutions with same aspect ratio")
        return clip_path, current_mbytes, current_scale
    # logging.debug("Possible lower resolutions %s", resolutions)
    # assuming rightmost element is next lowest
    new_scale = resolutions[-1]
    logging.info("Downscaling from %d:%d to %s", width, height, new_scale)
    new_clip = os.path.join(tmpdir, os.path.basename(clip_path))
    scale_cmd = ['ffmpeg', '-i', clip_path, '-vf', f'scale={new_scale}', new_clip]
    logging.debug(" ".join(scale_cmd))
    try:
        result = subprocess.run(scale_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        logging.exception(e)
        return clip_path, current_mbytes, 0
    if result.returncode != 0:
        logging.error(result.stderr.decode("utf-8"))
        return clip_path, current_mbytes, 0

    new_mbytes = os.stat(new_clip).st_size/2**20
    logging.info("New size is %.2fMb, Old size was %.2fMb", new_mbytes, current_mbytes)
    return new_clip, new_mbytes, new_scale

def get_duration(clip_path):

    result = subprocess.run(['ffprobe', '-show_format', '-v', 'quiet',
                            '-of', 'json', clip_path],
                            stdout=subprocess.PIPE).stdout.decode('utf-8')
    return float(json.loads(result)['format']['duration'])                  

def chunk_clip(args, camera, clip_path, tmpdir, chunk_group=1):

    # https://stackoverflow.com/questions/38259544/using-ffmpeg-to-split-video-files-by-size
    clip_base = os.path.basename(clip_path).split('.')[0]
    duration = get_duration(clip_path)
    mbytes = os.stat(clip_path).st_size / 2**20
    logging.info("Chunking %s %.2fMb, %.2f seconds", clip_base, mbytes, duration)
    current_duration = 0
    chunks = []
    chunk = 0
    while current_duration < duration:
        chunk_path = os.path.join(tmpdir, f"{clip_base}%{chunk}%.mp4")
        chunk += 1
        if chunk > args.max_chunks:
            logging.warning("Too many clips")
            caption = f"{camera} - Only posting {args.max_chunks} clips from {clip_base}, {mbytes:.2f}Mb"
            frame_path = get_frame(clip_path, tmpdir, 24)
            if frame_path:
                post_image(args, frame_path, caption)
            break
        chunk_cmd = ['ffmpeg', '-ss', str(current_duration), '-i', clip_path,
                    '-fs', str((int(args.max_telegram_mbytes * 2**20))), '-c', 'copy', chunk_path]
        logging.debug(chunk_cmd)
        try:
            result = subprocess.run(chunk_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            logging.exception(e)
            return []

        if result.returncode != 0:
            logging.error(result.stderr.decode("utf-8"))
            return []

        new_duration = get_duration(chunk_path)
        current_duration += new_duration
        # last chunk may be too small to bother with
        if os.stat(chunk_path).st_size < 2**20:
            break
        chunks.append(chunk_path)

    chunks = sorted([c for c in os.listdir(tmpdir) if f"{clip_base}%" in c])

    return chunks

def get_frame(clip_path, tmpdir, frame=2):

    frame_path = os.path.join(tmpdir, 'frame.png')
    clip_cmd = ['ffmpeg', '-i', clip_path, '-vf', f"select=eq(n\\,{frame})",
                '-vframes', '1', frame_path]
    logging.debug(clip_cmd)
    try:
        result = subprocess.run(clip_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except:
        logging.exception(e)
        return None

    if result.returncode != 0:
        logging.error(result.stderr.decode("utf-8"))
        return None

    return frame_path

def housekeep(args):

    now = time.time()
    clips = glob.iglob(f'{args.root}/**/*.mp4', recursive=True)
    for clip in clips:
        attrs = os.stat(clip)
        age_in_days = (now - attrs.st_mtime) / 86400
        if age_in_days > args.max_days_to_keep:
            logging.info("Removing %s which is %.2f days old", clip.replace(root,""), age_in_days)
            os.remove(clip)

def rename_clips(args, clips, tmpdir):

    new_clips = []
    for clip in clips:
        clip['name'] = clip['path'].replace(args.root,"")[1:]
        clip['camera'] = clip['name'].split('/')[0]
        logging.info("Found '%s', modified %.2f seconds ago, %.2f Mbytes",
                        clip['name'],
                        clip['age_in_seconds'], clip['Mbytes'])
        # move to format Saturday-03-April@06:18.21.mp4
        clip['name']= datetime.fromtimestamp(clip['mtime']).strftime('%A-%d-%B@%H:%M.%S') + '.mp4'
        new_path = os.path.dirname(clip['path']) + '/' + clip['name']
        logging.info("Moving %s", clip['path'])
        logging.info("to :   %s", new_path)
        if not args.dummy_run:
            os.rename(clip['path'], new_path)
            clip['path'] = new_path
            clip_name = clip['path'].replace(args.root,"")[1:]
        new_clips.append(clip)
    
    return new_clips

def process_clips(args, clips, tmpdir):

    for clip in rename_clips(args, clips, tmpdir):

        clip_name = clip['name']
        clip_base = clip_name.split('.')[0]
        camera = clip['camera'].capitalize()
        if clip['Mbytes'] < args.min_telegram_mbytes:
            logging.warning("%s is too small to post [%.2f]Mb", clip_base, clip['Mbytes'])
            continue
        if clip['Mbytes'] > args.max_telegram_mbytes:
            new_clip, mbytes, new_scale = downscale(args, clip['path'], clip_name, tmpdir)
            new_scale = f",{new_scale}" if new_scale else ""
            if new_clip == clip['path'] or mbytes > args.max_telegram_mbytes:
                chunks = chunk_clip(args, camera, new_clip, tmpdir)
                chunks = sorted(chunks, key = lambda x: int(x.split('%')[1].split('.')[0]))
                c = 0
                for chunk in chunks:
                    c += 1
                    chunk_path = os.path.join(tmpdir, chunk)
                    mb = os.stat(chunk_path).st_size / 2**20
                    caption = f"{camera} - {clip_base} [{c} of {len(chunks)}], {mb:.2f}Mb {new_scale}"
                    post_video(args, chunk_path, caption)
                    os.remove(chunk_path)
            else:
                caption = f"{camera} - {clip_base}, {mbytes:.2f}Mb {new_scale}"
                post_video(args, new_clip, caption)
                os.remove(new_clip)
                continue
        else:
            caption = f"{camera} - {clip_base}, {clip['Mbytes']:.2f}Mb"
            post_video(args, clip['path'], caption)

def main():

    with open(os.path.join(os.path.dirname(sys.argv[0]), 'sweep.json')) as c:
        config = json.loads(c.read())
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="Root directory to search for clips",
                         default=config.get("root","/var/lib/motioneye"))
    parser.add_argument("--max_age_seconds", help="Max age to scan for",
                         default=config.get("max_age_seconds", 600))
    parser.add_argument("--max_telegram_mbytes", help="Max clip size to post",
                         default=config.get("max_telegram_mbytes", 9))
    parser.add_argument("--min_telegram_mbytes", help="Min clip size to post",
                         default=config.get("min_telegram_mbytes", 0.5))
    parser.add_argument("--max_chunks", help="Limit on number of chunks per clip",
                         default=config.get("max_chunks", 20))
    parser.add_argument("--max_days_to_keep", help="Housekeep old clips",
                         default=config.get("max_days_to_keep", 3))
    parser.add_argument("--downscale", action='store_true', help="Don't attempt to downscale")
    parser.add_argument("--dummy_run", action='store_true', help="Don't post to Telegram")
    parser.add_argument("--debug", action='store_true', help="Debug level logging")
    args = parser.parse_args()
    loglevel = logging.INFO if not args.debug else logging.DEBUG
    logging.basicConfig(format='%(levelname)s:%(asctime)s %(message)s', level=loglevel)
    logging.info("%s Beginning Sweep %s", "#"*30, "#"*30)
    housekeep(args)

    args.resolutions = config['resolutions']
    args.bot = telegram.Bot(token=config['bot'])
    args.chat_id = config['chat_id']

    clips = find_new_clips(args)
    if len(clips) == 0:
        logging.info("No new clips to process")
        return
    with TemporaryDirectory() as tmpdir:
        try:
            process_clips(args, clips, tmpdir)
        except Exception as e:
            logging.exception(e)

if __name__ == "__main__":
    main()
