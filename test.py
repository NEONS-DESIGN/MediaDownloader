import tempfile

import streamlit as st

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

print(create_temp_cookie_file());