import asyncio
import os
import re
import shutil

from aiogram import types, Router, F
from aiogram.types import FSInputFile, InlineQueryResultVideo
from moviepy import VideoFileClip, AudioFileClip
from yt_dlp import YoutubeDL

import keyboards as kb
import messages as bm
from config import OUTPUT_DIR, CHANNEL_ID
from handlers.user import update_info
from log.logger import logger as logging
from main import bot, db, send_analytics

MAX_FILE_SIZE = 1 * 1024 * 1024

router = Router()


def get_ffmpeg_location():
    """Знаходить шлях до ffmpeg та ffprobe в системі"""
    # Спочатку перевіряємо через shutil.which (стандартний шлях)
    ffmpeg_path = shutil.which('ffmpeg')
    ffprobe_path = shutil.which('ffprobe')

    # Якщо обидва знайдені, повертаємо шлях до ffmpeg
    if ffmpeg_path and ffprobe_path:
        return ffmpeg_path

    # Перевіряємо шляхи pip інсталяції
    import sys
    pip_paths = [
        os.path.join(sys.prefix, 'bin'),
        os.path.join(sys.prefix, 'Scripts'),
        os.path.join(os.path.dirname(sys.executable), 'bin'),
        os.path.join(os.path.dirname(sys.executable), 'Scripts')
    ]

    for pip_path in pip_paths:
        ffmpeg_pip = os.path.join(pip_path, 'ffmpeg')
        ffprobe_pip = os.path.join(pip_path, 'ffprobe')

        if os.path.exists(ffmpeg_pip) and os.path.exists(ffprobe_pip):
            return ffmpeg_pip

        # Для Windows перевіряємо з розширенням .exe
        ffmpeg_win = os.path.join(pip_path, 'ffmpeg.exe')
        ffprobe_win = os.path.join(pip_path, 'ffprobe.exe')

        if os.path.exists(ffmpeg_win) and os.path.exists(ffprobe_win):
            return ffmpeg_win

    # Типові шляхи для пошуку ffmpeg
    possible_paths = [
        '/usr/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        '/opt/homebrew/bin/ffmpeg',  # Для macOS з Homebrew
        'C:\\ffmpeg\\bin\\ffmpeg.exe',  # Для Windows
    ]

    for path in possible_paths:
        # Перевіряємо і ffmpeg, і ffprobe в тих самих директоріях
        if os.path.isfile(path):
            ffprobe_path = path.replace('ffmpeg', 'ffprobe')
            if os.path.isfile(ffprobe_path):
                return path

    logging.warning("ffmpeg або ffprobe не знайдено. Спробуйте встановити через pip: pip install ffmpeg-python")
    return None


def download_video_yt_dlp(url, output_path, is_audio_only=False, quality='360p'):
    ffmpeg_location = get_ffmpeg_location()
    if not ffmpeg_location:
        logging.error("ffmpeg або ffprobe не знайдено в системі. Встановіть через pip або apt-get")
        return None, None, None, None, None, None

    # Отримуємо шлях до каталогу, де знаходиться ffmpeg
    ffmpeg_dir = os.path.dirname(ffmpeg_location)

    # Вибір формату в залежності від якості, завжди віддаємо перевагу MP4
    if is_audio_only:
        format_selector = 'bestaudio[ext=m4a]'
    else:
        if quality == '1080p':
            format_selector = 'bestvideo[height<=1080]+bestaudio[ext=m4a]/best[height<=1080]/best[height<=1080]'
        elif quality == '720p':
            format_selector = 'bestvideo[height<=720]+bestaudio[ext=m4a]/best[height<=720]/best[height<=720]'
        elif quality == '360p':
            format_selector = 'bestvideo[height<=360]+bestaudio[ext=m4a]/best[height<=360]/best[height<=360]'
        else:  # best or default
            format_selector = 'best[height<=1080]/best[height<=1080]'

    ydl_opts = {
        'outtmpl': os.path.join(output_path, '%(id)s_youtube_video.%(ext)s'),
        'format': format_selector,
        'ffmpeg_location': ffmpeg_dir,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }] if is_audio_only else [{
            # Впевнюємось, що кінцевий результат буде в MP4
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'quiet': False,
        'no_warnings': False,
    }

    if is_audio_only:
        ydl_opts['outtmpl'] = os.path.join(output_path, '%(id)s_youtube_audio.%(ext)s')

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get('id')
            title = info.get('title')
            views = info.get('view_count', 0)
            likes = info.get('like_count', 0)
            comments = info.get('comment_count', 0)

            if is_audio_only:
                return f"{video_id}_youtube_audio.mp3", title, video_id, views, likes, comments

            # Для відео завжди повертаємо з розширенням MP4
            return f"{video_id}_youtube_video.mp4", title, video_id, views, likes, comments
    except Exception as e:
        logging.error(f"Download error: {e}")
        return None, None, None, None, None, None


async def send_chat_action_if_needed(chat_id, action, business_id):
    if not business_id:
        await bot.send_chat_action(chat_id, action)


async def handle_download_error(message, business_id):
    if business_id is None:
        await message.react([types.ReactionTypeEmoji(emoji="👎")])
    await message.reply(bm.something_went_wrong())


async def get_youtube_info(url):
    """
    Отримує інформацію про YouTube відео без завантаження
    
    :param url: URL відео
    :return: Dictionary з інформацією про відео (title, video_id, views, likes, comments, thumbnail_url, quality_options)
    """
    try:
        with YoutubeDL({'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Базова інформація
            video_info = {
                'canonical_url': info.get('webpage_url') or info.get('original_url') or url,
                'video_id': info.get('id', ''),
                'title': info.get('title', 'YouTube Video'),
                'views': info.get('view_count', 0),
                'likes': info.get('like_count', 0),
                'comments': info.get('comment_count', 0),
                'thumbnail': info.get('thumbnail') or "https://www.freepnglogos.com/uploads/youtube-logo-png-22.png",
                'available_formats': []
            }
            
            # Перевіряємо доступні якості
            has_1080p = any(
                f.get('height') == 1080 for f in info.get('formats', []) if isinstance(f, dict) and f.get('height'))
            has_720p = any(
                f.get('height') == 720 for f in info.get('formats', []) if isinstance(f, dict) and f.get('height'))

            if has_1080p:
                video_info['available_formats'].append('1080p')
            if has_720p:
                video_info['available_formats'].append('720p')
            video_info['available_formats'].append('360p')  # Завжди додаємо низьку якість
            
            return video_info
    except Exception as e:
        logging.error(f"Error extracting info for YouTube video: {e}")
        # Повертаємо базову інформацію в разі помилки
        return {
            'canonical_url': url,
            'video_id': '',
            'title': 'YouTube Video',
            'views': 0,
            'likes': 0,
            'comments': 0,
            'thumbnail': "https://www.freepnglogos.com/uploads/youtube-logo-png-22.png",
            'available_formats': ['360p']
        }


@router.message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
@router.business_message(F.text.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
async def download_video(message: types.Message):
    url = message.text
    business_id = message.business_connection_id
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name="youtube_video")
    try:
        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        # Отримуємо інформацію про відео
        video_info = await get_youtube_info(url)
        canonical_url = video_info['canonical_url']
        title = video_info['title']
        views = video_info['views']
        likes = video_info['likes']
        comments = video_info['comments']
        available_formats = video_info['available_formats']

        # Якщо доступно кілька форматів, запитуємо користувача
        if len(available_formats) > 1 and not business_id:
            # Створюємо клавіатуру з доступними якостями
            markup = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"{quality}", callback_data=f"quality_{quality}_{canonical_url}")
                 for quality in available_formats]
            ])
            await message.reply("Choose video quality:", reply_markup=markup)
            return

        # Якщо тільки один формат або бізнес режим, завантажуємо відразу
        quality = available_formats[0] if available_formats else '360p'

        # Перевіряємо, чи є вже такий файл у базі даних з обраною якістю
        db_file_id = await db.get_file_id(f"{canonical_url}_{quality}")
        if db_file_id:
            await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
            await message.answer_video(
                video=db_file_id,
                caption=bm.captions(await db.get_user_captions(message.from_user.id), title,
                                    f"t.me/{(await bot.get_me()).username}"),
                reply_markup=kb.return_video_info_keyboard(
                    views, likes, comments, None, None, canonical_url
                ) if not business_id else None,
                parse_mode="HTML"
            )
            return

        # Завантажуємо відео
        filename, downloaded_title, video_id, updated_views, updated_likes, updated_comments = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_video_yt_dlp(url, OUTPUT_DIR, False, quality)
        )

        # Використовуємо дані з завантаження, якщо вони доступні
        if downloaded_title:
            title = downloaded_title
        if updated_views:
            views = updated_views
        if updated_likes:
            likes = updated_likes
        if updated_comments:
            comments = updated_comments

        if not filename:
            await message.reply(bm.nothing_found())
            return

        video_file_path = os.path.join(OUTPUT_DIR, filename)
        video_clip = VideoFileClip(video_file_path)

        file_size = os.path.getsize(video_file_path) / 1024
        if file_size >= MAX_FILE_SIZE:
            await message.reply(bm.video_too_large())
            return

        await send_chat_action_if_needed(message.chat.id, "upload_video", business_id)
        sent_message = await message.answer_video(
            video=FSInputFile(video_file_path),
            width=video_clip.w,
            height=video_clip.h,
            caption=bm.captions(await db.get_user_captions(message.from_user.id), title,
                                f"t.me/{(await bot.get_me()).username}"),
            reply_markup=kb.return_video_info_keyboard(
                views, likes, comments, None, None, canonical_url
            ) if not business_id else None,
            parse_mode="HTML"
        )
        # Зберігаємо файл за канонічним URL з якістю
        await db.add_file(f"{canonical_url}_{quality}", sent_message.video.file_id, "video")

        await asyncio.sleep(5)
        os.remove(video_file_path)
    except Exception as e:
        logging.error(f"Video download error: {e}")
        await handle_download_error(message, business_id)
    await update_info(message)


@router.message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
@router.business_message(F.text.regexp(r'(https?://)?(music\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/.+'))
async def download_music(message: types.Message):
    url = message.text
    business_id = message.business_connection_id
    try:
        if business_id is None:
            await message.react([types.ReactionTypeEmoji(emoji="👨‍💻")])

        # Отримуємо канонічний URL
        canonical_url = None
        try:
            with YoutubeDL({'skip_download': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                canonical_url = info.get('webpage_url') or info.get('original_url') or url
        except Exception as e:
            logging.error(f"Error extracting canonical URL for music: {e}")
            canonical_url = url

        # Перевіряємо, чи є вже такий аудіо файл у базі даних
        db_file_id = await db.get_file_id(f"{canonical_url}")
        if db_file_id:
            await send_chat_action_if_needed(message.chat.id, "upload_voice", business_id)
            await message.answer_audio(
                audio=db_file_id,
                caption=bm.captions(None, None, f"t.me/{(await bot.get_me()).username}"),
                parse_mode="HTML"
            )
            return

        filename, title, video_id, views, likes, comments = await asyncio.get_event_loop().run_in_executor(
            None, download_video_yt_dlp, url, OUTPUT_DIR, True
        )

        if not filename:
            await message.reply(bm.nothing_found())
            return

        audio_file_path = os.path.join(OUTPUT_DIR, filename)
        audio_duration = AudioFileClip(audio_file_path).duration

        await send_chat_action_if_needed(message.chat.id, "upload_voice", business_id)
        sent_message = await message.answer_audio(
            audio=FSInputFile(audio_file_path),
            title=title,
            duration=round(audio_duration),
            caption=bm.captions(None, None, f"t.me/{(await bot.get_me()).username}"),
            parse_mode="HTML"
        )
        # Зберігаємо за канонічним URL
        await db.add_file(f"{canonical_url}", sent_message.audio.file_id, "audio")

        await asyncio.sleep(5)
        os.remove(audio_file_path)
    except Exception as e:
        logging.error(f"Audio download error: {e}")
        await handle_download_error(message, business_id)
    await update_info(message)


@router.inline_query(F.query.regexp(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)"))
async def inline_youtube_query(query: types.InlineQuery):
    try:
        await send_analytics(user_id=query.from_user.id, chat_type=query.chat_type, action_name="inline_youtube_video")
        user_captions = await db.get_user_captions(query.from_user.id)
        bot_url = f"t.me/{(await bot.get_me()).username}"

        url_match = re.search(r"(https?://(?:www\.)?(?:youtube|youtu|youtube-nocookie)\.(?:com|be)/\S+)", query.query)
        if not url_match:
            return await query.answer([], cache_time=1, is_personal=True)

        url = url_match.group(0)
        
        # Перевіряємо, чи це YouTube Shorts
        is_shorts = '/shorts/' in url

        # Якщо це не шортс, повертаємо повідомлення про непідтримку
        if not is_shorts:
            not_supported_result = [
                types.InlineQueryResultArticle(
                    id="youtube_not_supported",
                    title="❌ Regular YouTube Videos Not Supported",
                    description="Only YouTube Shorts are supported in inline mode. Regular videos might be too large.",
                    input_message_content=types.InputTextMessageContent(
                        message_text=f"⚠️ Regular YouTube videos are not supported in inline mode due to size limitations. Please send the link directly to the bot: {bot_url}"
                    )
                )
            ]
            await query.answer(not_supported_result, cache_time=300, is_personal=True)
            return

        results = []

        # Отримуємо інформацію про відео
        video_info = await get_youtube_info(url)
        canonical_url = video_info['canonical_url']
        video_id = video_info['video_id']
        title = video_info['title']
        views = video_info['views']
        likes = video_info['likes']
        comments = video_info['comments']
        thumbnail = video_info['thumbnail']
        available_formats = video_info['available_formats']

        # Вибір найкращої доступної якості
        best_quality = available_formats[0] if available_formats else '360p'

        # Перевіряємо, чи є вже такий файл у базі даних з найкращою якістю
        db_file_id = await db.get_file_id(f"{canonical_url}_{best_quality}")

        # Якщо немає найкращої якості, перевіряємо наявність інших якостей
        if not db_file_id:
            for quality in available_formats[1:]:
                alternative_file_id = await db.get_file_id(f"{canonical_url}_{quality}")
                if alternative_file_id:
                    db_file_id = alternative_file_id
                    best_quality = quality
                    break

        if db_file_id:
            results.append(
                InlineQueryResultVideo(
                    id=f"video_{video_id or 'youtube'}_{best_quality}",
                    video_url=db_file_id,
                    thumbnail_url=thumbnail,
                    description=f"{title} - {best_quality}",
                    title=f"🎥 YouTube Shorts - {best_quality}",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, title, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views, likes, comments, None, None, canonical_url
                    )
                )
            )
            await query.answer(results, cache_time=300, is_personal=True)
        else:
            # Запускаємо завантаження у фоновому режимі
            await asyncio.create_task(process_youtube_download(query, url, canonical_url, video_id, title,
                                                               views, likes, comments, thumbnail, user_captions,
                                                               bot_url, best_quality))


    except Exception as e:
        logging.error(f"Error in inline_youtube_query: {e}")
        await query.answer([], cache_time=1, is_personal=True)


async def process_youtube_download(query, url, canonical_url, video_id, title, views, likes, comments,
                                   thumbnail, user_captions, bot_url, quality='360p'):
    """Обробляє завантаження YouTube відео в фоновому режимі і оновлює інлайн результат"""
    try:
        # Перевіряємо, чи це YouTube Shorts
        is_shorts = '/shorts/' in url or '/shorts/' in canonical_url
        
        # Якщо це не шортс, повертаємо повідомлення про непідтримку
        if not is_shorts:
            not_supported_result = [
                types.InlineQueryResultArticle(
                    id="youtube_not_supported",
                    title="❌ Regular YouTube Videos Not Supported",
                    description="Only YouTube Shorts are supported in inline mode. Regular videos might be too large.",
                    input_message_content=types.InputTextMessageContent(
                        message_text=f"⚠️ Regular YouTube videos are not supported in inline mode due to size limitations. Please send the link directly to the bot: {bot_url}"
                    )
                )
            ]
            await query.answer(not_supported_result, cache_time=300, is_personal=True)
            return
            
        # Завантажуємо відео
        filename, updated_title, updated_video_id, updated_views, updated_likes, updated_comments = \
            await asyncio.get_event_loop().run_in_executor(None, lambda: download_video_yt_dlp(url, OUTPUT_DIR, False,
                                                                                               quality))

        # Оновлюємо дані, якщо отримали нові
        if updated_title:
            title = updated_title
        if updated_video_id:
            video_id = updated_video_id
        if updated_views:
            views = updated_views
        if updated_likes:
            likes = updated_likes
        if updated_comments:
            comments = updated_comments

        if filename:
            video_file_path = os.path.join(OUTPUT_DIR, filename)
            video = FSInputFile(video_file_path)

            # Відправляємо в канал і отримуємо file_id
            sent_message = await bot.send_video(
                chat_id=CHANNEL_ID,
                video=video,
                caption=f"🎥 YouTube Shorts ({quality}) from {query.from_user.full_name}"
            )
            video_file_id = sent_message.video.file_id
            # Зберігаємо файл за канонічним URL з якістю
            await db.add_file(f"{canonical_url}_{quality}", video_file_id, "video")

            # Створюємо фінальний результат
            final_results = [
                InlineQueryResultVideo(
                    id=f"video_{video_id or 'youtube'}_{quality}",
                    video_url=video_file_id,
                    thumbnail_url=thumbnail,
                    description=f"{title} - {quality}",
                    title=f"🎥 YouTube Shorts - {quality}",
                    mime_type="video/mp4",
                    caption=bm.captions(user_captions, title, bot_url),
                    reply_markup=kb.return_video_info_keyboard(
                        views, likes, comments, None, None, canonical_url
                    )
                )
            ]

            # Надсилаємо оновлений результат
            await query.answer(final_results, cache_time=300, is_personal=True)

            # Прибираємо тимчасовий файл
            await asyncio.sleep(5)
            try:
                os.remove(video_file_path)
            except Exception as e:
                logging.error(f"Error removing file {video_file_path}: {e}")
        else:
            error_result = [
                types.InlineQueryResultArticle(
                    id="youtube_error",
                    title="❌ Download Error",
                    description="Failed to download this YouTube Shorts. Try another link or use the bot directly.",
                    input_message_content=types.InputTextMessageContent(
                        message_text=f"⚠️ Failed to download YouTube Shorts. Try using the bot directly: {bot_url}"
                    )
                )
            ]
            await query.answer(error_result, cache_time=30, is_personal=True)
    except Exception as e:
        logging.error(f"Error in process_youtube_download: {e}")
        error_result = [
            types.InlineQueryResultArticle(
                id="youtube_error",
                title="❌ Download Error",
                description="Failed to download this YouTube video. Try another link or use the bot directly.",
                input_message_content=types.InputTextMessageContent(
                    message_text=f"⚠️ Failed to download YouTube video. Try using the bot directly: {bot_url}"
                )
            )
        ]
        await query.answer(error_result, cache_time=30, is_personal=True)


@router.callback_query(F.data.startswith('quality_'))
async def download_video_with_quality(callback: types.CallbackQuery):
    """Завантажує відео в обраній якості"""
    try:
        # Отримуємо якість та URL
        parts = callback.data.split('_', 2)
        quality = parts[1]  # 1080p, 720p, 360p
        canonical_url = parts[2]

        # Перевіряємо, чи є вже такий файл у базі даних з обраною якістю
        db_file_id = await db.get_file_id(f"{canonical_url}_{quality}")
        if db_file_id:
            # Отримуємо інформацію про відео
            video_info = await get_youtube_info(canonical_url)
            title = video_info['title']
            views = video_info['views']
            likes = video_info['likes']
            comments = video_info['comments']

            # Видаляємо повідомлення про вибір якості
            await callback.message.delete()

            # Відправляємо відео з кешу
            await callback.message.answer_video(
                video=db_file_id,
                caption=bm.captions(await db.get_user_captions(callback.from_user.id), title,
                                    f"t.me/{(await bot.get_me()).username}"),
                reply_markup=kb.return_video_info_keyboard(
                    views, likes, comments, None, None, canonical_url
                ),
                parse_mode="HTML"
            )
            return

        await callback.answer(f"Downloading in {quality}...")

        # Повідомляємо про початок завантаження
        message = await callback.message.edit_text(f"Downloading video in {quality}...")

        # Отримуємо інформацію про відео (для отримання title, views тощо)
        video_info = await get_youtube_info(canonical_url)
        
        # Завантажуємо відео обраної якості
        filename, title, video_id, views, likes, comments = await asyncio.get_event_loop().run_in_executor(
            None, lambda: download_video_yt_dlp(canonical_url, OUTPUT_DIR, False, quality)
        )

        # Використовуємо дані з video_info, якщо завантаження не повернуло ці значення
        if not title and video_info['title']:
            title = video_info['title']
        if not views and video_info['views']:
            views = video_info['views']
        if not likes and video_info['likes']:
            likes = video_info['likes']
        if not comments and video_info['comments']:
            comments = video_info['comments']

        if not filename:
            await callback.message.edit_text(bm.nothing_found())
            return

        video_file_path = os.path.join(OUTPUT_DIR, filename)
        video_clip = VideoFileClip(video_file_path)

        file_size = os.path.getsize(video_file_path) / 1024
        if file_size >= MAX_FILE_SIZE:
            await callback.message.edit_text(bm.video_too_large())
            return

        await send_chat_action_if_needed(message.chat.id, "upload_video", None)
        # Відправляємо відео
        sent_message = await callback.message.answer_video(
            video=FSInputFile(video_file_path),
            width=video_clip.w,
            height=video_clip.h,
            caption=bm.captions(await db.get_user_captions(callback.from_user.id), title,
                                f"t.me/{(await bot.get_me()).username}"),
            reply_markup=kb.return_video_info_keyboard(
                views, likes, comments, None, None, canonical_url
            ),
            parse_mode="HTML"
        )

        # Зберігаємо файл за канонічним URL з якістю
        await db.add_file(f"{canonical_url}_{quality}", sent_message.video.file_id, "video")

        # Видаляємо повідомлення про вибір якості
        await callback.message.delete()

        # Видаляємо тимчасовий файл
        await asyncio.sleep(5)
        os.remove(video_file_path)

    except Exception as e:
        logging.error(f"Error downloading video with specific quality: {e}")
        await callback.message.edit_text(bm.something_went_wrong())
