import os

import streamlit as st
import yt_dlp
import uuid
import time
import shutil
import threading
import tempfile
import re
import base64
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 1. アプリケーション設定 (一括管理)
# ==========================================
class APP_CONFIG:
	TEMP_DIR = Path("temp_storage")      # 一時保存ディレクトリ
	MAX_WORKERS = 3                      # 同時にダウンロード・変換処理を行う最大数（サーバー負荷調整用）
	FILE_LIFETIME_SEC = 1800             # ファイルの保持時間 (秒) = 30分
	CLEANUP_INTERVAL_SEC = 300           # 自動お掃除の巡回間隔 (秒) = 5分
	HISTORY_TITLE_LIMIT = 25             # サイドバー履歴のタイトル最大文字数
	POLLING_RATE = 0.5                   # プログレスバーの更新間隔 (秒)

# 起動時に一時保存ディレクトリを作成
APP_CONFIG.TEMP_DIR.mkdir(exist_ok=True)


# ==========================================
# 2. セッション・グローバル変数の初期化
# ==========================================
st.set_page_config(page_title="メディアダウンローダー", layout="wide")
st.set_page_config(initial_sidebar_state="expanded")

# UI状態の管理
if "history" not in st.session_state: st.session_state.history = []
if "video_info" not in st.session_state: st.session_state.video_info = None
if "is_loading" not in st.session_state: st.session_state.is_loading = False

# スレッド間で進捗状況を共有するための辞書（st.session_stateへの直接書き込みエラー防止用）
if "shared_progress" not in st.session_state:
	st.session_state.shared_progress = {}


# ==========================================
# 3. バックグラウンド管理 (キュー・自動清掃)
# ==========================================
@st.cache_resource
def get_executor():
	"""タスクを順番に処理するためのキュー管理オブジェクトを生成"""
	return ThreadPoolExecutor(max_workers=APP_CONFIG.MAX_WORKERS)

def cleanup_worker():
	"""古いファイルを定期的に削除するバックグラウンドスレッド"""
	while True:
		now = time.time()
		for path in APP_CONFIG.TEMP_DIR.glob("*"):
			# 指定時間を過ぎたディレクトリを丸ごと削除
			if path.is_dir() and (now - path.stat().st_mtime > APP_CONFIG.FILE_LIFETIME_SEC):
				shutil.rmtree(path, ignore_errors=True)
		time.sleep(APP_CONFIG.CLEANUP_INTERVAL_SEC)

# メインプログラムの邪魔にならないよう、裏側(デーモン)で掃除スレッドを起動
if not any(t.name == "CleanupThread" for t in threading.enumerate()):
	threading.Thread(target=cleanup_worker, name="CleanupThread", daemon=True).start()


# ==========================================
# 4. ヘルパー関数 (バリデーション・エラー翻訳・Cookie処理)
# ==========================================
def is_valid_url(url):
	"""入力文字列が一般的なURLの形式をしているかチェック"""
	pattern = re.compile(r'^https?://[\w/:%#\$&\?\(\)~\.=\+\-]+')
	return bool(pattern.match(url))

def translate_error(e):
	"""yt-dlpなどのシステムエラーを、ユーザー向けの分かりやすい日本語に翻訳"""
	err_str = str(e).lower()
	if "incompleteread" in err_str or "connection" in err_str:
		return "通信が切断されました。ネットワーク環境を確認し、再度お試しください。"
	if "403" in err_str or "forbidden" in err_str:
		return "アクセスが拒否されました。動画サイト側の制限、またはIPブロックの可能性があります。"
	if "not available" in err_str or "private" in err_str:
		return "この動画は現在利用できないか、非公開に設定されています。"
	if "ffmpeg" in err_str:
		return "音声/映像の変換処理(ffmpeg)でエラーが発生しました。別の拡張子をお試しください。"
	if "unsupported url" in err_str:
		return "このサイトのURLには対応していません。"
	# 該当しない場合は元エラーの先頭部分を表示
	return f"予期しないエラーが発生しました: {str(e)[:100]}..."

def create_temp_cookie_file():
	"""Streamlit SecretsからCookie情報を読み込み、一時ファイルを作成する"""
	try:
		if hasattr(st, "secrets") and "YOUTUBE_COOKIES" in st.secrets:
			# delete=Falseで作成し、使い終わったら手動で消す（Windows環境でのアクセスエラー回避のため）
			tf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8')
			tf.write(st.secrets["YOUTUBE_COOKIES"])
			tf.close()
			return tf.name
	except Exception as e:
		print(f"Cookie loading error: {e}")
	return None

def get_base_ydl_opts(cookie_path):
	"""Cookie, PO Token, Visitor Data を統合した最強のオプション設定"""
	# 1. Streamlit Secrets からトークンを取得
	# ※ 事前に Secrets 側に PO_TOKEN と VISITOR_DATA を登録しておく必要があります
	po_token = st.secrets.get("PO_TOKEN")
	visitor_data = st.secrets.get("VISITOR_DATA")

	# 2. 抽出引数（extractor_args）の組み立て
	youtube_dict = {
        # 'ios' がCookie非対応でスキップされるため、'android' や 'tv' を追加して優先順位を変更
        'player_client': ['android', 'tv', 'web', 'mweb'],
        'player_skip': [],
    }

	if po_token and visitor_data:
		# トークンがある場合のみ追加（書式: "web+トークン"）
		youtube_dict['po_token'] = [f"web+{po_token}"]
		youtube_dict['visitor_data'] = [visitor_data]

	opts = {
		'nocolor': True,
		'quiet': False,
		'verbose': True,
		'js_runtimes': {'node': {}},
		'allow_remote_strings': True,
		'remote_components': ['ejs:github'],
		'headers': {
			'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
		},
		'extractor_args': {'youtube': youtube_dict},
		# 映像と音声を最高品質で結合
		'format': 'bestvideo+bestaudio/best',
	}

	if cookie_path:
		opts['cookiefile'] = cookie_path

	return opts


# ==========================================
# 5. メインUI (ヘッダーとURL入力)
# ==========================================
st.title("メディアダウンローダー")
st.markdown("YouTube, ニコニコ動画, X(旧Twitter), Vimeo,など様々なサイトの動画・音声を最高品質で保存できます。")

url_input = st.text_input("動画のURLを入力してください:", placeholder="https://...")

if st.button("コンテンツ情報を解析"):
	if not url_input:
		st.warning("URLが入力されていません。")
	elif not is_valid_url(url_input):
		st.error("入力された文字列は有効なURL形式ではありません。")
	else:
		# 古いデータを消去し、ロード中状態へ移行
		st.session_state.video_info = None
		st.session_state.is_loading = True

		with st.spinner("URLを検証してメタデータを取得中..."):
			cookie_path = create_temp_cookie_file()
			try:
				ydl_opts = get_base_ydl_opts(cookie_path)
				ydl_opts['noplaylist'] = True
				# download=False で実際のダウンロードは行わず情報だけ引き抜く
				with yt_dlp.YoutubeDL(ydl_opts) as ydl:
					info = ydl.extract_info(url_input, download=False)
					st.session_state.video_info = info
			except Exception as e:
				st.error(translate_error(e))
			finally:
				# 使い終わったCookieの一時ファイルを確実に削除
				if cookie_path and os.path.exists(cookie_path):
					os.remove(cookie_path)

		st.session_state.is_loading = False


# ==========================================
# 6. 解析結果の表示セクション
# ==========================================
if st.session_state.is_loading:
	st.info("⌛ 情報を取得しています。少々お待ちください...")
	st.progress(100) # ダミーのロードバー

elif st.session_state.video_info:
	info = st.session_state.video_info
	st.divider()

	col_left, col_right = st.columns([1.5, 1])

	# [左側] プレビュー領域
	with col_left:
		st.subheader(info.get('title', 'タイトル不明'))
		try:
			st.video(url_input)
		except:
			st.image(info.get('thumbnail'))
			st.caption("※このサイトはプレビュー埋め込みに非対応のため、サムネイルを表示しています。")

	# [右側] 統計データ領域
	with col_right:
		st.write("#### 📊 メディア統計")
		dur = str(timedelta(seconds=info.get('duration', 0)))
		views = info.get('view_count')
		likes = info.get('like_count')
		comments = info.get('comment_count')
		date = info.get('upload_date', '不明')
		f_date = f"{date[:4]}/{date[4:6]}/{date[6:]}" if len(date)==8 else date

		st.info(f"👤 **投稿者:** {info.get('uploader', '不明')}")

		m1, m2 = st.columns(2)
		with m1:
			st.metric("再生時間", dur)
			st.metric("いいね数", f"{likes:,}" if likes is not None else "非公開/不可")
		with m2:
			st.metric("視聴数", f"{views:,}" if views is not None else "---")
			st.metric("コメント数", f"{comments:,}" if comments is not None else "非公開/不可")
		st.caption(f"📅 投稿日: {f_date}")

	st.divider()

	# ==========================================
	# 7. ダウンロード設定セクション
	# ==========================================
	st.write("### 📥 保存設定")
	mode = st.radio("保存モードを選択", ["動画 (映像+音声)", "音声のみ", "映像のみ"], horizontal=True)

	c1, c2, c3 = st.columns(3)

	with c1:
		is_audio = (mode == "音声のみ")
		ext_options = ["mp3", "wav", "m4a", "flac"] if is_audio else ["mp4", "webm", "mkv"]
		ext = st.selectbox("形式 (拡張子)", ext_options)

	with c2:
		is_res_disabled = (mode == "音声のみ")
		# 取得できた解像度リスト（重複排除・降順）
		res_list = sorted(list(set([f.get('height') for f in info.get('formats',[]) if f.get('height')])), reverse=True)
		res_choice = st.selectbox(
			"解像度",
			[f"{r}p" for r in res_list] if res_list else ["best"],
			disabled=is_res_disabled,
			help="音声のみモードでは指定できません" if is_res_disabled else "希望の画質を選択"
		)

	with c3:
		is_abr_disabled = (mode == "映像のみ")
		abr_choice = st.select_slider(
			"音質 (kbps)",
			options=["128", "192", "256", "320"],
			value="192",
			disabled=is_abr_disabled,
			help="映像のみモードでは指定できません" if is_abr_disabled else "数値が高いほど高音質です"
		)

	# ==========================================
	# 8. 実行と自動ダウンロード処理
	# ==========================================
	if st.button("ダウンロードを開始", type="primary"):
		job_id = str(uuid.uuid4())[:8]
		user_dir = APP_CONFIG.TEMP_DIR / job_id
		user_dir.mkdir()
		st.session_state.shared_progress[job_id] = 0.0

		# --- スレッド内で実行されるタスク ---
		def download_task(url, mode, ext, res, abr, user_dir, j_id, progress_dict):
			# スレッド内でもCookieファイルを作成（処理中に消えないようにするため）
			task_cookie_path = create_temp_cookie_file()

			try:
				def hook(d):
					if d['status'] == 'downloading':
						p = d.get('downloaded_bytes', 0)
						t = d.get('total_bytes') or d.get('total_bytes_estimate', 1)
						progress_dict[j_id] = p / t

				ydl_opts = get_base_ydl_opts(task_cookie_path)
				ydl_opts['outtmpl'] = f'{user_dir}/%(title)s.%(ext)s'
				ydl_opts['progress_hooks'] = [hook]

				h = res.replace('p','') if res and 'p' in str(res) else 'best'
				audio_f = f"bestaudio[abr<={abr}]/bestaudio" if abr else "bestaudio"

				if mode == "動画 (映像+音声)":
					ydl_opts['format'] = f'bestvideo[height<={h}][ext={ext}]+{audio_f}/best[height<={h}]'
					ydl_opts['merge_output_format'] = ext
				elif mode == "映像のみ":
					ydl_opts['format'] = f'bestvideo[height<={h}][ext={ext}]/bestvideo'
				else:
					ydl_opts['format'] = audio_f
					ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': ext, 'preferredquality': abr}]

				with yt_dlp.YoutubeDL(ydl_opts) as ydl:
					res_info = ydl.extract_info(url, download=True)
					return ydl.prepare_filename(res_info)
			finally:
				# 処理完了後、スレッド用のCookieファイルも削除
				if task_cookie_path and os.path.exists(task_cookie_path):
					os.remove(task_cookie_path)
		# --------------------------------------

		executor = get_executor()
		# スレッドプールにタスクを投入
		future = executor.submit(
			download_task, url_input, mode, ext, res_choice, abr_choice,
			user_dir, job_id, st.session_state.shared_progress
		)

		progress_ui = st.empty()
		status_ui = st.empty()

		# スレッドが完了するまでメインUIを更新し続ける（ポーリング）
		while not future.done():
			prog = st.session_state.shared_progress.get(job_id, 0.0)
			progress_ui.progress(min(prog, 1.0), text=f"処理中... {prog*100:.1f}%")
			time.sleep(APP_CONFIG.POLLING_RATE)

		# 処理完了後のアクション
		try:
			final_path_str = future.result()
			final_path = Path(final_path_str)
			# 音声変換で拡張子が変わった場合の補正
			if mode == "音声のみ" and not final_path.suffix.endswith(ext):
				final_path = final_path.with_suffix(f".{ext}")

			# 履歴に追加
			st.session_state.history.append({
				"title": info['title'],
				"path": str(final_path),
				"time": datetime.now().strftime("%H:%M:%S"),
				"desc": f"{mode} | {res_choice if mode != '音声のみ' else '-'} | {abr_choice if mode != '映像のみ' else '-'}kbps"
			})

			progress_ui.empty()
			status_ui.success("✅ 処理完了！自動ダウンロードを開始します。")

			# --- 自動ダウンロード用 JavaScriptインジェクション ---
			# ※注意: 超巨大なファイル(GB単位)の場合、メモリ不足になる可能性があります。
			with open(final_path, "rb") as f:
				b64 = base64.b64encode(f.read()).decode()
				safe_filename = final_path.name.replace('"', '\\"')
				dl_script = f"""
					<a id="auto_dl_{job_id}" href="data:application/octet-stream;base64,{b64}" download="{safe_filename}"></a>
					<script>
						document.getElementById('auto_dl_{job_id}').click();
					</script>
				"""
				st.components.v1.html(dl_script, height=0)

		except Exception as e:
			progress_ui.empty()
			status_ui.error(translate_error(e))


# ==========================================
# 9. サイドバー (履歴と再ダウンロード)
# ==========================================
with st.sidebar:
	st.header("📜 ダウンロード履歴")
	st.caption(f"※ サーバー保護のため、{APP_CONFIG.FILE_LIFETIME_SEC // 60}分経過したファイルは自動で削除されます。")

	if not st.session_state.history:
		st.write("まだ履歴はありません。")

	# 新しい順に表示
	for idx, item in enumerate(reversed(st.session_state.history)):
		display_title = item['title']
		if len(display_title) > APP_CONFIG.HISTORY_TITLE_LIMIT:
			display_title = display_title[:APP_CONFIG.HISTORY_TITLE_LIMIT] + "..."

		with st.expander(f"[{item['time'][:5]}] {display_title}"):
			st.caption(item['desc'])
			p = Path(item['path'])

			if p.exists():
				with open(p, "rb") as f:
					st.download_button(
						label="💾 もう一度PCに保存",
						data=f,
						file_name=p.name,
						key=f"dl_btn_{idx}_{p.stem}",
						use_container_width=True
					)
			else:
				st.error("自動清掃により削除されました")