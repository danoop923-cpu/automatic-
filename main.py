from flask import Flask, request, render_template_string, redirect, url_for, send_from_directory
from threading import Thread
import requests, time, os, itertools, random
from pathlib import Path
from werkzeug.utils import secure_filename
from PIL import Image

app = Flask(__name__)
app.secret_key = "change_this_secret_in_prod"

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

TOKENS_PATH = os.path.join(UPLOAD_FOLDER, "tokens.txt")
TEXTS_PATH = os.path.join(UPLOAD_FOLDER, "text.txt")
PHOTO_LIST_PATH = os.path.join(UPLOAD_FOLDER, "photo.txt")
VIDEO_LIST_PATH = os.path.join(UPLOAD_FOLDER, "video.txt")
CAPTION_PATH = os.path.join(UPLOAD_FOLDER, "caption.txt")
TAGS_PATH = os.path.join(UPLOAD_FOLDER, "tags.txt")  # unlimited tags/mentions
COMMENTS_PATH = os.path.join(UPLOAD_FOLDER, "comments.txt")  # comments to post line-by-line

valid_tokens = []
token_index = 0
is_running = False
posting_thread = None
current_status = "Stopped"
recent_logs = []

# Comments iterator (cycle through lines)
_comments_iter = None
_comments_last_mtime = None

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry)
    global recent_logs
    recent_logs = ([entry] + recent_logs)[:500]  # bigger log buffer

def save_text_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        if content: f.write(content.strip() + ("\n" if not content.endswith("\n") else ""))

def append_list_file(path, items):
    existing = []
    if os.path.exists(path):
        existing = [l.strip() for l in open(path,"r",encoding="utf-8") if l.strip()]
    new = existing + items
    with open(path,"w",encoding="utf-8") as f:
        for i in new: f.write(i+"\n")

def load_lines(path):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

def validate_tokens_file(path):
    tokens = load_lines(path)
    good = []
    log(f"Validating {len(tokens)} tokens...")
    for i,t in enumerate(tokens,start=1):
        try:
            r = requests.get(f"https://graph.facebook.com/me?access_token={t}", timeout=10).json()
            if 'id' in r:
                good.append(t)
                log(f"[{i}] VALID: {r.get('name')}")
            else:
                log(f"[{i}] INVALID: {r.get('error',{}).get('message','Unknown')}")
        except Exception as e:
            log(f"[{i}] INVALID: {e}")
    return good

def next_token():
    global token_index
    if not valid_tokens:
        raise RuntimeError("No valid tokens available")
    token = valid_tokens[token_index % len(valid_tokens)]
    token_index += 1
    return token

def get_tags():
    if os.path.exists(TAGS_PATH):
        lines = [l.strip() for l in open(TAGS_PATH,"r",encoding="utf-8") if l.strip()]
        return ",".join(lines)
    return ""

def image_to_ascii(path,width=80):
    chars = "@%#*+=-:. "
    try:
        img = Image.open(path).convert('L')
        wpercent = (width/float(img.size[0]))
        hsize = int((float(img.size[1])*float(wpercent)))
        img = img.resize((width, hsize))
        pixels = img.getdata()
        ascii_str = "".join([chars[pixel//25] for pixel in pixels])
        ascii_lines = [ascii_str[i:i+width] for i in range(0,len(ascii_str),width)]
        return "\n".join(ascii_lines)
    except Exception as e:
        log(f"ASCII conversion failed: {e}")
        return "[Image ASCII Conversion Failed]"

def post_text_fb(token,message):
    tags = get_tags()  # unlimited mentions/tags
    url='https://graph.facebook.com/me/feed'
    payload={'message':message,'privacy':'{"value":"EVERYONE"}','access_token':token}
    if tags: payload['tags']=tags
    return requests.post(url,data=payload,timeout=30)

def upload_video_fb(token,file_path,caption):
    tags = get_tags()
    url='https://graph.facebook.com/me/videos'
    with open(file_path,'rb') as fd:
        files={'file':fd}
        payload={'access_token':token,'description':caption}
        if tags: payload['tags']=tags
        return requests.post(url,data=payload,files=files,timeout=180)

# ---------------------------
# Comments support functions
# ---------------------------
def _load_comments_iter(force_reload=False):
    """
    Load comments.txt and return a cycling iterator.
    We reload if file modified (mtime) or if iterator not created.
    """
    global _comments_iter, _comments_last_mtime
    try:
        if not os.path.exists(COMMENTS_PATH):
            _comments_iter = itertools.cycle([""])  # empty comment if not present
            _comments_last_mtime = None
            return _comments_iter
        mtime = os.path.getmtime(COMMENTS_PATH)
        if force_reload or _comments_iter is None or _comments_last_mtime != mtime:
            lines = [l.strip() for l in open(COMMENTS_PATH,"r",encoding="utf-8") if l.strip()]
            if not lines:
                lines = [""]
            _comments_iter = itertools.cycle(lines)
            _comments_last_mtime = mtime
            log(f"Comments loaded ({len(lines)} lines).")
    except Exception as e:
        log(f"Failed to load comments: {e}")
        _comments_iter = itertools.cycle([""])
    return _comments_iter

def get_next_comment():
    it = _load_comments_iter()
    try:
        c = next(it)
        return c
    except Exception as e:
        log(f"Error cycling comment: {e}")
        return ""

def send_comment_to_post(token, post_id, comment_text):
    """
    Send comment to a post id (Facebook Graph API).
    Uses same token to comment so it appears from same account.
    """
    if not comment_text:
        return None
    url = f"https://graph.facebook.com/{post_id}/comments"
    payload = {'message': comment_text, 'access_token': token}
    try:
        r = requests.post(url, data=payload, timeout=20)
        try:
            jr = r.json()
        except:
            jr = {'error': 'non-json-response'}
        if r.status_code in (200,201) and 'id' in jr:
            log(f"AUTO COMMENT SENT on {post_id}: {comment_text[:80]}")
            return jr
        else:
            log(f"AUTO COMMENT FAILED on {post_id}: {jr}")
            return jr
    except Exception as e:
        log(f"EXC AUTO COMMENT: {e}")
        return None

# ---------------------------
# Posting worker (modified + fixes)
# ---------------------------
def posting_worker(post_type,delay_seconds):
    global is_running,current_status
    log(f"Worker started: type={post_type} delay={delay_seconds}s")
    current_status="Running"
    try:
        if not valid_tokens:
            log("No valid tokens to proceed. Worker exiting.")
            return

        if post_type=="text":
            posts=load_lines(TEXTS_PATH)
            if not posts: log("No text posts found."); return
            while is_running:
                for text in posts:
                    if not is_running: break
                    try:
                        token=next_token()
                    except Exception as e:
                        log(f"Token error: {e}"); is_running=False; break
                    try:
                        res=post_text_fb(token,text)
                        jr=res.json()
                        if 'id' in jr:
                            post_id = jr.get('id')
                            log(f"TEXT POSTED: {text[:60]}... id={post_id}")
                            # --- AUTO COMMENT (same token) ---
                            comment_text = get_next_comment()
                            time.sleep(random.randint(2,4))
                            send_comment_to_post(token, post_id, comment_text)
                        else:
                            log(f"TEXT ERROR: {jr}")
                    except Exception as e: log(f"EXC TEXT: {e}")
                    time.sleep(delay_seconds)

        elif post_type=="photo":
            media_entries=load_lines(PHOTO_LIST_PATH)
            captions=load_lines(CAPTION_PATH)
            pairs=[]
            for i,name in enumerate(media_entries):
                full=os.path.join(UPLOAD_FOLDER,name)
                if os.path.exists(full):
                    caption=captions[i] if i<len(captions) else ""
                    ascii_text = image_to_ascii(full)
                    pairs.append({'text':ascii_text,'caption':caption})
                else: log(f"Missing media file: {name}")
            if not pairs: log("No valid media found. Stopping worker."); return
            while is_running:
                for item in pairs:
                    if not is_running: break
                    try:
                        token=next_token()
                    except Exception as e:
                        log(f"Token error: {e}"); is_running=False; break
                    msg=f"{item['caption']}\n\n{item['text']}"
                    try:
                        res=post_text_fb(token,msg)
                        jr=res.json()
                        if 'id' in jr:
                            post_id = jr.get('id')
                            log(f"PHOTO AS TEXT POSTED id={post_id}")
                            # auto comment
                            comment_text = get_next_comment()
                            time.sleep(random.randint(2,4))
                            send_comment_to_post(token, post_id, comment_text)
                        else:
                            log(f"PHOTO AS TEXT ERROR: {jr}")
                    except Exception as e: log(f"EXC PHOTO AS TEXT: {e}")
                    time.sleep(delay_seconds)

        elif post_type=="video":
            media_entries=load_lines(VIDEO_LIST_PATH)
            captions=load_lines(CAPTION_PATH)
            pairs=[]
            for i,name in enumerate(media_entries):
                full=os.path.join(UPLOAD_FOLDER,name)
                if os.path.exists(full):
                    caption=captions[i] if i<len(captions) else ""
                    pairs.append({'path':full,'caption':caption})
                else: log(f"Missing video file: {name}")
            if not pairs: log("No valid videos found. Stopping worker."); return
            while is_running:
                for item in pairs:
                    if not is_running: break
                    try:
                        token=next_token()
                    except Exception as e:
                        log(f"Token error: {e}"); is_running=False; break
                    try:
                        r=upload_video_fb(token,item['path'],item['caption'])
                        jr=r.json()
                        if 'id' in jr:
                            post_id = jr.get('id')
                            log(f"VIDEO POSTED: {Path(item['path']).name} id={post_id}")
                            # auto comment
                            comment_text = get_next_comment()
                            time.sleep(random.randint(2,4))
                            send_comment_to_post(token, post_id, comment_text)
                        else:
                            log(f"VIDEO ERROR: {jr}")
                    except Exception as e: log(f"EXC VIDEO: {e}")
                    time.sleep(delay_seconds)
    finally:
        is_running=False
        current_status="Stopped"
        log("Worker stopped.")

# Premium modern dashboard HTML (glassmorphism + animated gradient)
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Ayush Auto Poster — Premium</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{
  --glass-bg: rgba(255,255,255,0.04);
  --glass-border: rgba(255,255,255,0.08);
  --accent1: #00ff99;
  --accent2: #00ccff;
  --muted: rgba(255,255,255,0.6);
  --glass-blur: 8px;
  --card-radius: 14px;
}

/* animated gradient background */
html,body{
  height:100%;
  margin:0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  background: linear-gradient(120deg, #021125 0%, #06122a 35%, #001219 100%);
  color: #e6fff5;
  overflow-y: auto;
}
.bg-anim {
  position: fixed;
  inset: -20%;
  background: radial-gradient(circle at 10% 20%, rgba(0,255,153,0.06), transparent 8%),
              radial-gradient(circle at 80% 70%, rgba(0,204,255,0.06), transparent 8%),
              radial-gradient(circle at 50% 40%, rgba(255,255,255,0.02), transparent 20%);
  filter: blur(40px);
  animation: floatBG 14s ease-in-out infinite;
  z-index:0;
  pointer-events:none;
}
@keyframes floatBG {
  0% { transform: translateY(0) scale(1); }
  50% { transform: translateY(-20px) scale(1.06); }
  100% { transform: translateY(0) scale(1); }
}

/* container / glass cards */
.container {
  position:relative;
  z-index:2;
}
.glass {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--card-radius);
  box-shadow: 0 8px 30px rgba(0,0,0,0.6);
}

/* heading */
.header {
  display:flex;
  align-items:center;
  gap:12px;
  margin:18px 0;
}
.logo {
  width:56px;height:56px;border-radius:12px;background:linear-gradient(135deg,var(--accent1),var(--accent2));
  display:flex;align-items:center;justify-content:center;font-weight:700;color:#001;
  box-shadow:0 6px 18px rgba(0,0,0,0.6);
}

/* tabs styling */
.nav-tabs .nav-link {
  color:var(--muted);
  border: none;
  margin-right:6px;
  background: transparent;
  transition: all .18s ease-in-out;
}
.nav-tabs .nav-link.active {
  color: white;
  background: linear-gradient(90deg,var(--accent1),var(--accent2));
  border-radius: 10px;
  box-shadow: 0 6px 18px rgba(0,0,0,0.5);
}

/* forms */
textarea,input,select,button{background:transparent;color:var(--muted);border:1px dashed rgba(255,255,255,0.06);}
.form-control:focus{outline:none;box-shadow:none;border-color:rgba(255,255,255,0.14);color:#e6fff5;}
.small-muted{color:rgba(255,255,255,0.55);font-size:0.86rem;}

/* logs */
pre { background: rgba(0,0,0,0.35); color: #d9fff0; padding:12px; border-radius:10px; max-height:360px; overflow:auto; }

/* file list */
.file-list li { margin-bottom:6px; }
.badge-video { background: linear-gradient(90deg,#ff7a7a,#ffb26b); color:#001; border-radius:999px; padding:4px 8px; }

/* controls */
.controls .btn { border-radius:10px; padding:10px 14px; }

/* footer */
.footer { margin-top:18px; color:var(--muted); font-size:0.86rem; }

/* responsive tweaks */
@media (max-width:767px){
  .header { gap:8px }
  .logo { width:48px;height:48px }
}
</style>
</head>
<body>
<div class="bg-anim" aria-hidden="true"></div>
<div class="container my-4">
  <div class="header">
    <div class="logo">AA</div>
    <div>
      <h3 style="margin:0">Ayush Auto Poster — Premium</h3>
      <div class="small-muted">Multi-account posting • ASCII photo mode • Auto-comments</div>
    </div>
    <div class="ms-auto small-muted text-end">
      Status: <strong style="color:var(--accent1)">{{status}}</strong><br>
      Port: <small>21378</small>
    </div>
  </div>

  <div class="row g-3">
    <div class="col-lg-8">
      <div class="p-3 glass">
        <ul class="nav nav-tabs" id="mainTab" role="tablist">
          <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tokens">Tokens</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#text">Text</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#media">Media</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#captions">Captions</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tags">Tags</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#comments">Comments</button></li>
          <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#logs">Logs</button></li>
        </ul>

        <div class="tab-content mt-3">
          <div class="tab-pane fade show active" id="tokens">
            <form method="POST" action="/upload_tokens" enctype="multipart/form-data">
              <textarea name="tokens" rows="4" class="form-control mb-2" placeholder="One token per line"></textarea>
              <input type="file" name="tokens_file" class="form-control mb-2">
              <button type="submit" class="btn btn-light w-100 controls">Save Tokens</button>
            </form>
            <div class="small-muted mt-2">Tip: use one token per line. Tokens are validated on Start.</div>
          </div>

          <div class="tab-pane fade" id="text">
            <form method="POST" action="/upload_text" enctype="multipart/form-data">
              <textarea name="texts" rows="5" class="form-control mb-2" placeholder="One text per line"></textarea>
              <input type="file" name="text_file" class="form-control mb-2">
              <button type="submit" class="btn btn-light w-100 controls">Save Texts</button>
            </form>
          </div>

          <div class="tab-pane fade" id="media">
            <form method="POST" action="/upload_media" enctype="multipart/form-data">
              <input type="file" name="media_files" multiple class="form-control mb-2">
              <button type="submit" class="btn btn-light w-100 controls">Upload Media</button>
            </form>
            <h6 class="mt-3">Uploaded Files</h6>
            <ul class="file-list">
              {% for f in files %}
                <li>
                  <a href="/uploads/{{f}}" target="_blank">{{f}}</a>
                  {% if f.lower().endswith(('.mp4','.mov','.mkv','.avi')) %}
                    <span class="badge-video ms-2">VIDEO</span>
                  {% endif %}
                </li>
              {% endfor %}
            </ul>
          </div>

          <div class="tab-pane fade" id="captions">
            <form method="POST" action="/upload_captions" enctype="multipart/form-data">
              <textarea name="captions" rows="4" class="form-control mb-2" placeholder="One caption per line"></textarea>
              <input type="file" name="caption_file" class="form-control mb-2">
              <button type="submit" class="btn btn-light w-100 controls">Save Captions</button>
            </form>
          </div>

          <div class="tab-pane fade" id="tags">
            <form method="POST" action="/upload_tags">
              <textarea name="tags" rows="3" class="form-control mb-2" placeholder="Comma-separated IDs (unlimited mentions)"></textarea>
              <button type="submit" class="btn btn-light w-100 controls">Save Tags</button>
            </form>
            <div class="small-muted mt-2">Example: 123456789,987654321</div>
          </div>

          <div class="tab-pane fade" id="comments">
            <form method="POST" action="/upload_comments" enctype="multipart/form-data">
              <textarea name="comments" rows="5" class="form-control mb-2" placeholder="One comment per line (auto-cycle)"></textarea>
              <input type="file" name="comments_file" class="form-control mb-2">
              <button type="submit" class="btn btn-light w-100 controls">Save Comments</button>
            </form>
            <div class="small-muted mt-2">Comments are used line-by-line and cycle automatically.</div>
          </div>

          <div class="tab-pane fade" id="logs">
            <pre>{% for l in logs %}{{l}}\n{% endfor %}</pre>
          </div>
        </div>
      </div>

      <div class="mt-3 glass p-3">
        <h5 class="mb-3">Controls</h5>
        <form method="POST" action="/start" class="row g-2 align-items-center">
          <div class="col-6 col-md-4">
            <select name="post_type" class="form-select">
              <option value="text">Text</option>
              <option value="photo">Photo (ASCII)</option>
              <option value="video">Video</option>
            </select>
          </div>
          <div class="col-4 col-md-3">
            <input type="number" name="delay" value="30" min="1" class="form-control" placeholder="Delay seconds">
          </div>
          <div class="col-2 col-md-5">
            <button type="submit" class="btn btn-success w-100 controls">Start Posting</button>
          </div>
        </form>
        <form method="POST" action="/stop" class="mt-2">
          <button type="submit" class="btn btn-danger w-100 controls">Stop Posting</button>
        </form>
      </div>

    </div>

    <div class="col-lg-4">
      <div class="glass p-3 mb-3">
        <h6>Quick Info</h6>
        <div class="small-muted">Files folder: <code>{{upload_folder}}</code></div>
        <div class="small-muted mt-2">Tokens validated on clicking Start. Logs show recent activity.</div>
        <div class="footer mt-3">Made for Ayush • Keep tokens safe.</div>
      </div>

      <div class="glass p-3">
        <h6>Shortcuts</h6>
        <div class="d-grid gap-2">
          <a class="btn btn-outline-light" href="/">Refresh</a>
          <a class="btn btn-outline-light" href="/uploads">Open Uploads</a>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

@app.route("/")
def index():
    files=sorted(os.listdir(UPLOAD_FOLDER),reverse=True)
    return render_template_string(INDEX_HTML, files=files, status=current_status, logs=recent_logs, upload_folder=UPLOAD_FOLDER)

@app.route("/uploads/<path:filename>")
def uploaded(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False)

@app.route("/upload_tokens", methods=["POST"])
def upload_tokens():
    txt=request.form.get("tokens","").strip()
    file=request.files.get("tokens_file")
    if file and file.filename:
        file.save(os.path.join(UPLOAD_FOLDER,"tokens.txt")); log("tokens.txt uploaded")
    elif txt: save_text_file(TOKENS_PATH,txt); log("tokens.txt saved from textarea")
    return redirect(url_for("index"))

@app.route("/upload_text", methods=["POST"])
def upload_text():
    txt=request.form.get("texts","").strip()
    file=request.files.get("text_file")
    if file and file.filename:
        file.save(os.path.join(UPLOAD_FOLDER,"text.txt")); log("text.txt uploaded")
    elif txt: save_text_file(TEXTS_PATH,txt); log("text.txt saved from textarea")
    return redirect(url_for("index"))

@app.route("/upload_media", methods=["POST"])
def upload_media():
    files=request.files.getlist("media_files")
    saved_names=[]
    video_exts = ('.mp4','.mov','.mkv','.avi')
    saved_videos=[]
    saved_photos=[]
    for f in files:
        if f and f.filename:
            name=secure_filename(f.filename)
            f.save(os.path.join(UPLOAD_FOLDER,name))
            saved_names.append(name)
            log(f"Saved media file: {name}")
            if name.lower().endswith(video_exts):
                saved_videos.append(name)
            else:
                saved_photos.append(name)
    if saved_photos:
        append_list_file(PHOTO_LIST_PATH,saved_photos)
        log(f"Appended {len(saved_photos)} files to {PHOTO_LIST_PATH}")
    if saved_videos:
        append_list_file(VIDEO_LIST_PATH,saved_videos)
        log(f"Appended {len(saved_videos)} files to {VIDEO_LIST_PATH}")
    return redirect(url_for("index"))

@app.route("/upload_captions", methods=["POST"])
def upload_captions():
    txt=request.form.get("captions","").strip()
    file=request.files.get("caption_file")
    if file and file.filename:
        file.save(os.path.join(UPLOAD_FOLDER,"caption.txt")); log("caption.txt uploaded")
    elif txt: save_text_file(CAPTION_PATH,txt); log("caption.txt saved from textarea")
    return redirect(url_for("index"))

@app.route("/upload_tags", methods=["POST"])
def upload_tags():
    txt=request.form.get("tags","").strip()
    if txt: save_text_file(TAGS_PATH,txt); log("tags.txt saved")
    return redirect(url_for("index"))

# NEW: Upload comments (either textarea or file)
@app.route("/upload_comments", methods=["POST"])
def upload_comments():
    txt=request.form.get("comments","").strip()
    file=request.files.get("comments_file")
    if file and file.filename:
        file.save(os.path.join(UPLOAD_FOLDER,"comments.txt")); log("comments.txt uploaded")
    elif txt: save_text_file(COMMENTS_PATH,txt); log("comments.txt saved from textarea")
    # Force reload comments iterator next time
    _load_comments_iter(force_reload=True)
    return redirect(url_for("index"))

@app.route("/start", methods=["POST"])
def start():
    global valid_tokens, posting_thread, is_running, token_index
    post_type=request.form.get("post_type","text")
    delay=int(request.form.get("delay",30))
    if not os.path.exists(TOKENS_PATH): log("No tokens.txt found."); return redirect(url_for("index"))
    valid_tokens=validate_tokens_file(TOKENS_PATH)
    if not valid_tokens: log("No valid tokens after validation."); return redirect(url_for("index"))
    token_index=0
    if is_running: log("Worker already running."); return redirect(url_for("index"))
    # ensure comments iterator exists
    _load_comments_iter(force_reload=True)
    is_running=True
    posting_thread=Thread(target=posting_worker,args=(post_type,delay),daemon=True)
    posting_thread.start()
    log("Posting started.")
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    global is_running
    if is_running: is_running=False; log("Stop requested.")
    else: log("Worker not running.")
    return redirect(url_for("index"))

if __name__=="__main__":
    # keep host/port same as you used earlier
    app.run(host="0.0.0.0", port=5000)

