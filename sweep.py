import os
import sys
import logging
import argparse
import glob
import time
import subprocess
import json
import asyncio
import telegram
from datetime import datetime
from tempfile import TemporaryDirectory

"""
Script updated by Gemini to work with python-telegram-bot 22.6
"""

# --- ASYNC TELEGRAM HELPERS ---

async def post_video(args, clip_path, caption):
    logging.info("Posting %s to Telegram '%s'", clip_path, caption)
    if args.dummy_run:
        return
    try:
        with open(clip_path, 'rb') as video:
            # v20+ change: use 'await' and 'write_timeout'
            await args.bot.send_video(
                chat_id=args.chat_id,
                video=video,
                caption=caption,
                write_timeout=120, 
                read_timeout=60,
                disable_notification=True
            )
    except Exception as e:
        logging.exception("Error in post_video: %s", e)

async def post_image(args, image_path, caption):
    logging.info("Posting %s to Telegram '%s'", image_path, caption)
    if args.dummy_run:
        return
    try:
        with open(image_path, 'rb') as photo:
            await args.bot.send_photo(
                chat_id=args.chat_id,
                photo=photo,
                caption=caption,
                write_timeout=60,
                disable_notification=True
            )
    except Exception as e:
        logging.exception("Error in post_image: %s", e)

# --- CORE LOGIC FUNCTIONS ---

def get_duration(clip_path):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'json', clip_path],
                                stdout=subprocess.PIPE).stdout.decode('utf-8')
        return float(json.loads(result)['format']['duration'])
    except Exception:
        return 0

def find_new_clips(args):
    now = time.time()
    clips = [c for c in glob.iglob(f'{args.root}/**/*.mp4', recursive=True) if '@' not in c]
    clips_to_process = []
    for clip in clips:
        attrs = os.stat(clip)
        age_in_seconds = now - attrs.st_mtime
        if age_in_seconds > args.max_age_seconds:
            logging.warning("%s is too old (%.2f seconds)", clip, age_in_seconds)
            continue
        if age_in_seconds < args.min_age_seconds:
            logging.warning("%s is too young (%.2f seconds)", clip, age_in_seconds)
            continue
        clips_to_process.append({
            'path': clip, 
            'age_in_seconds': age_in_seconds,
            'mtime': attrs.st_mtime,
            'Mbytes': attrs.st_size/2**20
        })
    return sorted(clips_to_process, key=lambda x: x['age_in_seconds'], reverse=True)

def downscale(args, clip_path, clip_name, tmpdir):
    current_mbytes = os.stat(clip_path).st_size/2**20
    if not args.downscale or current_mbytes <= args.max_telegram_mbytes:
        return clip_path, current_mbytes, None
    try:
        res_proc = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                   '-show_entries', 'stream=width,height',
                                   '-of', 'json', clip_path],
                                   stdout=subprocess.PIPE)
        result = res_proc.stdout.decode('utf-8')
        data = json.loads(result)
        width = data['streams'][0]['width']
        height = data['streams'][0]['height']
    except Exception as e:
        logging.exception("Downscale ffprobe failed: %s", e)
        return clip_path, current_mbytes, None

    current_scale = f"{width}:{height}"
    aspect_ratio = round(width/height, 2)
    same_aspect = [r for r in args.resolutions if round(int(r.split(":")[0])/int(r.split(":")[1]), 2) == aspect_ratio]
    pixels = width * height
    resolutions = [r for r in same_aspect if int(r.split(":")[0])*int(r.split(":")[1]) < pixels]   
    
    if not resolutions:
        return clip_path, current_mbytes, current_scale

    new_scale = resolutions[-1]
    new_clip = os.path.join(tmpdir, os.path.basename(clip_path))
    scale_cmd = ['ffmpeg', '-y', '-i', clip_path, '-vf', f'scale={new_scale}', new_clip]
    
    result = subprocess.run(scale_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return clip_path, current_mbytes, 0

    new_mbytes = os.stat(new_clip).st_size/2**20
    return new_clip, new_mbytes, new_scale

def chunk_clip(args, camera, clip_path, tmpdir):
    clip_base = os.path.basename(clip_path).split('.')[0]
    duration = get_duration(clip_path)
    current_duration = 0
    chunks = []
    chunk_idx = 0
    
    while current_duration < duration:
        chunk_path = os.path.join(tmpdir, f"{clip_base}%{chunk_idx}%.mp4")
        if chunk_idx >= args.max_chunks:
            break
            
        chunk_cmd = ['ffmpeg', '-y', '-ss', str(current_duration), '-i', clip_path,
                    '-fs', str((int(args.max_telegram_mbytes * 2**20))), '-c', 'copy', chunk_path]
        
        result = subprocess.run(chunk_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0 or not os.path.exists(chunk_path) or os.stat(chunk_path).st_size < 1024*100: 
            break

        new_dur = get_duration(chunk_path)
        current_duration += new_dur
        chunks.append(chunk_path)
        chunk_idx += 1
        
    return chunks

def housekeep(args):
    now = time.time()
    for clip in glob.iglob(f'{args.root}/**/*.mp4', recursive=True):
        attrs = os.stat(clip)
        if (now - attrs.st_mtime) / 86400 > args.max_days_to_keep:
            logging.info("Housekeeping: Removing %s", clip)
            try:
                os.remove(clip)
            except Exception as e:
                logging.error("Failed to remove %s: %s", clip, e)

def rename_clips(args, clips):
    new_clips = []
    for clip in clips:
        # Get camera name from folder structure
        rel_path = clip['path'].replace(args.root,"").strip('/')
        clip['camera'] = rel_path.split('/')[0] if '/' in rel_path else "Camera"
        
        # New filename format: Saturday-03-April@06:18.21.mp4
        clip['name'] = datetime.fromtimestamp(clip['mtime']).strftime('%A-%d-%B@%H:%M.%S') + '.mp4'
        new_path = os.path.join(os.path.dirname(clip['path']), clip['name'])
        
        if not args.dummy_run:
            try:
                os.rename(clip['path'], new_path)
                clip['path'] = new_path
            except Exception as e:
                logging.error("Rename failed: %s", e)
        new_clips.append(clip)
    return new_clips

# --- ASYNC PROCESSOR ---

async def process_clips(args, clips, tmpdir):
    for clip in rename_clips(args, clips):
        clip_base = clip['name'].split('.')[0]
        camera = clip['camera'].capitalize()

        # FIXED LINE 177: Added missing colon
        if clip['Mbytes'] < args.min_telegram_mbytes:
            continue
            
        duration = get_duration(clip['path'])
        if duration < 2:
            continue

        target_file = clip['path']
        mbytes = clip['Mbytes']
        scale_info = ""

        # Handle Downscaling if file is too large
        if mbytes > args.max_telegram_mbytes:
            new_file, new_mb, scale = downscale(args, clip['path'], clip['name'], tmpdir)
            if new_file != clip['path']:
                target_file = new_file
                mbytes = new_mb
                scale_info = f", {scale}"

        # Handle Chunking if still too big for Telegram
        if mbytes > args.max_telegram_mbytes:
            chunks = chunk_clip(args, camera, target_file, tmpdir)
            for i, chunk_path in enumerate(chunks):
                mb = os.stat(chunk_path).st_size / 2**20
                caption = f"{camera} - {clip_base} [{i+1}/{len(chunks)}], {mb:.2f}Mb{scale_info}"
                await post_video(args, chunk_path, caption)
        else:
            caption = f"{camera} - {clip_base}, {mbytes:.2f}Mb{scale_info}"
            await post_video(args, target_file, caption)

# --- MAIN ENTRY POINT ---

async def main():
    # Load config from sweep.json in same directory as script
    config_file = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'sweep.json')
    with open(config_file) as f:
        config = json.load(f)

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=config.get("root", "/var/lib/motioneye"))
    parser.add_argument("--max_age_seconds", type=int, default=config.get("max_age_seconds", 600))
    parser.add_argument("--min_age_seconds", type=int, default=config.get("min_age_seconds", 30))
    parser.add_argument("--max_telegram_mbytes", type=float, default=config.get("max_telegram_mbytes", 45))
    parser.add_argument("--min_telegram_mbytes", type=float, default=config.get("min_telegram_mbytes", 0.5))
    parser.add_argument("--max_chunks", type=int, default=config.get("max_chunks", 10))
    parser.add_argument("--max_days_to_keep", type=int, default=config.get("max_days_to_keep", 3))
    parser.add_argument("--downscale", action='store_true')
    parser.add_argument("--dummy_run", action='store_true')
    parser.add_argument("--debug", action='store_true')
    args = parser.parse_args()

    args.resolutions = config.get('resolutions', ["1280:720", "640:480"])
    args.chat_id = config['chat_id']

    loglevel = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(levelname)s: %(asctime)s %(message)s', level=loglevel)

    logging.info("Starting motion sweep...")
    housekeep(args)
    
    clips = find_new_clips(args)
    if not clips:
        logging.info("No new clips found to process.")
        return

    # Modern Bot initialization using a context manager
    async with telegram.Bot(token=config['bot']) as bot:
        args.bot = bot
        with TemporaryDirectory() as tmpdir:
            try:
                await process_clips(args, clips, tmpdir)
            except Exception as e:
                logging.exception("Failed during processing: %s", e)
    logging.info("Sweep complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
