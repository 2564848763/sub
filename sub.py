import os, re, sys, glob, shutil, subprocess, tempfile, time, threading, html, base64, requests, atexit
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse
from requests.adapters import HTTPAdapter

# ===== 全部从 GitHub Secrets 读取 (Groq Key已内置默认值) =====
WEBDAV_BASE = os.environ.get("WEBDAV_BASE", "https://webdav.123pan.cn/webdav").rstrip("/")
WEBDAV_USER = os.environ.get("WEBDAV_USER")
WEBDAV_PASS = os.environ.get("WEBDAV_PASS")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_KEY", "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")
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
ROOT = os.environ.get("ROOT", "视频/蔡斯")

# ASR 恢复为 whisper 保证时间轴绝对同步
ASR = (os.environ.get("ASR", "whisper") or "whisper").strip().lower()

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
ASR_MODEL = "mimo-v2.5-asr"
DEEP_MODEL = "deepseek-v4-flash"
REFINE = os.environ.get("REFINE", "true").lower() == "true" 
THINKING = os.environ.get("THINKING", "true").lower() == "true"
BUDGET = float('inf')
IN_PRICE, OUT_PRICE = 1e-6, 2e-6
TR_W = int(os.environ.get("TR_W", "8")); BATCH = 25
GLOSSARY = os.environ.get("GLOSSARY", "true").lower() == "true"
ITALIC_SFX = os.environ.get("ITALIC_SFX", "false").lower() == "true"
PREFETCH = os.environ.get("PREFETCH", "true").lower() == "true"
PARALLEL_DL = int(os.environ.get("PARALLEL_DL", "8"))
CHUNK_BYTES = 128 << 20
UPLOAD_VERIFY = True; AUDIO_ENHANCE = True; OVERLAP = 3; MIN_DUR = 1.0; SEG = 240 # 升级为4分钟大切片，大幅提升吞吐与上下文

if not (WEBDAV_USER and WEBDAV_PASS): sys.exit("❌ 缺少 WEBDAV_USER / WEBDAV_PASS")
if not (MIMO_API_KEY or DEEPSEEK_KEY): sys.exit("❌ 翻译密钥缺失")
if not shutil.which("ffmpeg"): os.system("sudo apt-get install -y -qq ffmpeg >/dev/null 2>&1")

AUTH = (WEBDAV_USER, WEBDAV_PASS)
VIDEO_EXT = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts','.mpg','.mpeg'}
COST = 0.0; _cached_in = 0; _prompt_in = 0; _auth_fail = 0; _dl_got = 0
_lock = threading.Lock(); _stop = threading.Event(); _failed_dirs = []; _CUR_GLOSS = ""

# ================= 123云盘防风控与极限连接池 =================
webdav_sess = requests.Session()
webdav_sess.auth = AUTH
adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=3)
webdav_sess.mount("http://", adapter)
webdav_sess.mount("https://", adapter)

webdav_sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Accept-Encoding": "gzip, deflate"
})
# =====================================================

# ================= 极致本土化神级提示词 =================
SYS = """你是顶级私密字幕组首席本地化主笔兼纠错专家。你的任务是将带有ASR误差的英文原始字幕，本地化为最符合【中国人真实口语习惯】、最具情感张力的简体中文成人字幕。

【核心神技：ASR脑补纠错】
机器语音识别极易出错。你必须根据上下文，自动纠正原文中的荒谬错词（例如把 f**k 听成 fact，把 c**k 听成 clock）后再翻译。绝不能逐字死翻！

【中式口语化翻译底线（最重要！）】
1. 抹除翻译腔：绝对拒绝“机翻感”。频繁省略主语（“我”、“你”），多用短句，根据情绪极其自然地融入中文语气词（啊、呢、嘛、哦、呀、哈）。
2. 场景化意译：不要把 F-word 永远死板地翻成同一个脏字。根据情节烈度，它可以是“天呐”、“妈的”、“受不了了”或者直接省略为急促的喘息。相关器官与动作要用国内成人语境中最地道、最直接的俚语。
3. 情感与节奏：人在极端情绪下说话是碎片化、重复的。例如原文的 "harder, faster" 必须翻译成“再用力点...快点...”。
4. 标点：禁用中文句号、逗号、顿号。短停顿用半角空格，长拖音或失语用三个英文句点 `...`。
5. 拟声词：纯喘息用全角方括号包裹，如【啊…】、【哈…】。若与文字混合，只包裹纯拟声部分。

【幻觉过滤】
若整句为套话或完全无意义的识别乱码，直接只输出 `...` 保留编号。

【格式铁律】
每行必须为：[编号] 译文。编号同行不换行，绝不输出任何额外解释。"""

SYS1 = """你是顶级成人字幕翻译。英文原文可能有错音字，请联系语境自动纠错后再翻译。要求：极其符合中国人真实口语习惯，多省略主语，巧用语气词。用语地道露骨，极简口语化，纯喘息用【】包裹。禁用中文句号逗号。仅输出译文，不要编号，不要解释。"""

SYS_REFINE = """你是精通中国本土互联网语境的字幕润色专家。在完全保留原意大尺度的前提下，抹除一切“翻译腔”，让初译文变得像真正的中国人在私密场景下的自然表达（多省略主语、增加自然语气词、符合中文节奏）。机器错词请直接根据语境修正。保留所有【】拟声标记和...拖音。禁用中文句号逗号。每行 [编号] 润色后译文，无任何解释。"""
# =====================================================

class LimitExceeded(RuntimeError): pass
def wurl(rel): return WEBDAV_BASE + "/" + quote(rel.strip("/"), safe="/")

def note_auth_fail(e):
    global _auth_fail; st = getattr(getattr(e, "response", None), "status_code", 0)
    if st in (401, 403):
        with _lock:
            _auth_fail += 1; 
            if _auth_fail >= 3: _stop.set()
        return True
    return False

def add_cost(u):
    global COST, _cached_in, _prompt_in
    with _lock:
        COST += u.get("prompt_tokens",0)*IN_PRICE + u.get("completion_tokens",0)*OUT_PRICE
        _prompt_in += u.get("prompt_tokens",0)
        _cached_in += (u.get("prompt_tokens_details") or {}).get("cached_tokens",0)
        if COST >= BUDGET: _stop.set()
    return COST >= BUDGET

# 智能重试机制：根据 Retry-After 动态休眠
def post_retry(url, **kw):
    for _ in range(5):
        try:
            r = requests.post(url, **kw)
            if r.status_code == 429: 
                wait = int(r.headers.get("Retry-After", 15))
                time.sleep(wait + 1); continue
            if r.status_code >= 500: 
                time.sleep(10); continue
            r.raise_for_status(); return r
        except requests.exceptions.RequestException: time.sleep(10)
    raise LimitExceeded("多次重试仍失败")

def req_retry(method, url, **kw):
    for _ in range(5):
        try:
            r = webdav_sess.request(method, url, **kw)
            if r.status_code == 429: 
                wait = int(r.headers.get("Retry-After", 15))
                time.sleep(wait + 1); continue
            if r.status_code >= 500: 
                time.sleep(10); continue
            r.raise_for_status(); return r
        except requests.exceptions.RequestException: time.sleep(10)
    raise LimitExceeded("多次请求失败")

def walk(rel):
    out, norm = [], rel.strip("/"); r = None
    for attempt in range(3):
        try: r = req_retry("PROPFIND", wurl(rel), headers={"Depth":"1"}, timeout=180); break
        except Exception as e: note_auth_fail(e); time.sleep(8); r = None
    if r is None: _failed_dirs.append(rel); return out
    for block in re.findall(r"<\w+:response\b.*?</\w+:response>", r.text, re.S):
        hm = re.search(r"<\w+:href>(.*?)</\w+:href>", block, re.S)
        if not hm: continue
        raw = urlparse(html.unescape(unquote(hm.group(1)))).path.split("/webdav",1)[-1]
        relp = raw.strip("/")
        if not relp or relp == norm: continue
        is_dir = bool(re.search(r"<\w+:collection\b", block, re.I)) or raw.endswith("/")
        if is_dir and relp != norm: out += walk(relp)
        elif not is_dir: out.append(relp)
    return out

def fmt(t):
    ms=int(round(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def sfx_render(t):
    if not isinstance(t, str): t = "" if t is None else str(t)
    return t.replace("【","<i>").replace("】","</i>") if ITALIC_SFX else t

def _cut(s, n):
    if len(s) <= n: return s
    i = s.rfind(" ", 1, n); return s[:i] if i > n//2 else s[:n]

def wrap_subtitle(text, max_len=42):
    text = (text or "").strip()
    if "\n" in text: lines = [l.strip() for l in text.split("\n") if l.strip()]; return "\n".join(lines[:2])
    if len(text) <= max_len: return text
    mid = len(text)//2; best = -1
    for i,ch in enumerate(text):
        if ch == " ":
            if best < 0 or abs(i-mid) < abs(best-mid): best = i
    if best > 0: l1, l2 = text[:best].strip(), text[best+1:].strip(); return _cut(l1, max_len) + "\n" + _cut(l2, max_len)
    return _cut(text, max_len) + "\n" + _cut(text[max_len:], max_len)

def polish_timing(segs):
    for i,s in enumerate(segs):
        if s['end'] <= s['start']: s['end'] = s['start']+MIN_DUR
        if s['end']-s['start'] < MIN_DUR:
            s['end'] = s['start']+MIN_DUR
            if i+1 < len(segs) and s['end'] > segs[i+1]['start']: s['end'] = segs[i+1]['start']
    return segs

def merge_segs(existing, new):
    for s in new:
        text_clean = re.sub(r'[^\w\s]', '', s['text']).lower().strip()
        if any(abs(s['start']-e['start']) < 3.5 and text_clean == re.sub(r'[^\w\s]', '', e['text']).lower().strip() for e in existing[-8:]): continue
        if existing and s['start'] < existing[-1]['end'] and len(s['text']) > len(existing[-1]['text']): existing[-1] = s
        else: existing.append(s)
    return existing

def distribute_text(text, start, end):
    text = (text or "").strip(); if not text: return []
    parts = [p.strip() for p in re.split(r'(?<=[。！？!?…])\s*|\n+', text) if p.strip()] or [text]
    total = end - start; totalc = sum(len(p) for p in parts) or 1
    out = []; base = start
    for p in parts: frac = len(p)/totalc; out.append({"start": base, "end": base+total*frac, "text": p}); base += total*frac
    return out

def probe_dur(local):
    try: out = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",local], check=True, capture_output=True, text=True); return float(out.stdout.strip() or 0)
    except: return 0.0

def transcribe_mimo(path, offset, dur, prompt="", language="en"):
    with open(path, "rb") as f: audio_base64 = base64.b64encode(f.read()).decode("utf-8")
    messages = [{"role":"user","content":[{"type":"input_audio","input_audio":{"data":f"data:audio/mpeg;base64,{audio_base64}"}}]}]
    body = {"model": ASR_MODEL, "messages": messages, "asr_options": {"language": language}}
    last = None
    for attempt in range(3):
        try:
            r = requests.post(f"{MIMO_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {MIMO_API_KEY}", "Content-Type": "application/json"}, json=body, timeout=600)
            if r.status_code == 429: 
                wait = int(r.headers.get("Retry-After", 15)); time.sleep(wait + 1); continue
            if r.status_code >= 500: 
                last = RuntimeError(f"HTTP {r.status_code}"); time.sleep(15); continue
            r.raise_for_status(); j = r.json(); text = j["choices"][0]["message"]["content"] if "choices" in j else j.get("text","")
            if isinstance(text, list): text = "".join(t.get("text","") for t in text if isinstance(t,dict))
            return distribute_text(text, offset, offset+dur)
        except requests.exceptions.RequestException as e: last = e; time.sleep(15)
    raise RuntimeError(f"小米 ASR 失败: {last}")

def transcribe_whisper(path, offset, prompt=""):
    nsfw_bias = "Intimate adult scene, heavy breathing, passionate moaning, explicit dialogue. "
    final_prompt = (nsfw_bias + prompt)[-1000:]
    
    with open(path, "rb") as f:
        r = post_retry("https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            data={"model":"whisper-large-v3","language":"en","response_format":"verbose_json","temperature":0.0,"prompt": final_prompt},
            files={"file": (os.path.basename(path), f, "audio/wav")}, timeout=900)
    out, prev = [], None
    for s in r.json().get("segments", []):
        if s.get("no_speech_prob",0) > 0.6 or s.get("avg_logprob",0) < -0.8: continue
        t = (s.get("text") or "").strip()
        if not t or len(t) < 2 or (t == prev and len(t) > 30): continue
        prev = t; out.append({"start": float(s.get("start",0))+offset, "end": float(s.get("end",0))+offset, "text": t})
    return out

def transcribe(path, offset, dur, prompt=""):
    order = {"mimo":["mimo","whisper"], "whisper":["whisper","mimo"]}.get(ASR, ["whisper","mimo"])
    last=None
    for b in order:
        try:
            if b=="mimo" and MIMO_API_KEY: return transcribe_mimo(path, offset, dur, prompt)
            if b=="whisper" and GROQ_KEY: return transcribe_whisper(path, offset, prompt)
        except Exception as e: last=e; print(f"  ⚠ {b} ASR 失败: {e}")
    raise RuntimeError(f"所有ASR均失败: {last}")

def _bump(g):
    global _dl_got
    with _lock: _dl_got += g; return _dl_got

def _dl_single(vp, path):
    got = os.path.getsize(path) if os.path.exists(path) else 0; total = 0
    while True:
        if _stop.is_set(): raise LimitExceeded("停止")
        try:
            with webdav_sess.get(wurl(vp), stream=True, headers={"Range": f"bytes={got}-"} if got > 0 else {}, timeout=3600) as r:
                if r.status_code == 416: break
                r.raise_for_status()
                total = got + int(r.headers.get("content-length",0)) if r.status_code == 206 else int(r.headers.get("content-length",0))
                with open(path, "ab" if r.status_code == 206 else "wb") as f:
                    for ch in r.iter_content(1<<16):
                        f.write(ch); got += len(ch)
                        if total: print(f"\r    下载 {got/1e6:.0f}/{total/1e6:.0f}MB",end="")
            if total == 0 or got >= total: break
        except requests.exceptions.RequestException: print(f"\n    ⚠ 下载续传..."); time.sleep(5); got = os.path.getsize(path) if os.path.exists(path) else 0
    print()

def _dl_parallel(vp, path, total):
    part = max(CHUNK_BYTES, total // max(1, PARALLEL_DL))
    ranges=[(s, min(s+part-1, total-1)) for s in range(0, total, part)]
    fd=os.open(path, os.O_CREAT|os.O_WRONLY, 0o644); os.ftruncate(fd, total)
    failed=[]; last=[0.0]
    def worker(rng):
        s,e=rng
        time.sleep((s % 10) * 0.05) 
        for attempt in range(4):
            if _stop.is_set(): return
            off=s
            try:
                with webdav_sess.get(wurl(vp), stream=True, headers={"Range":f"bytes={s}-{e}"}, timeout=3600) as r:
                    if r.status_code != 206: r.raise_for_status()
                    for ch in r.iter_content(1<<16):
                        os.pwrite(fd, ch, off); off += len(ch); now=_bump(len(ch))
                        if now-last[0] > 200e6 or now >= total: last[0]=now; print(f"\r    并行下载 {now/1e6:.0f}/{total/1e6:.0f}MB",end="")
                if off-1 >= e: return
            except: time.sleep(3)
        failed.append(rng)
    with ThreadPoolExecutor(max_workers=min(PARALLEL_DL, len(ranges))) as ex: list(ex.map(worker, ranges))
    os.close(fd); print()
    if _stop.is_set() or failed: raise LimitExceeded("并行下载失败或停止")

def dl(vp, path):
    global _dl_got
    _dl_got = 0; ok=False; total=0
    try:
        with webdav_sess.get(wurl(vp), stream=True, headers={"Range":"bytes=0-0"}, timeout=60) as r:
            r.raise_for_status(); ok = r.status_code == 206
            total = int(r.headers.get("content-range","").split("/")[-1] or 0) if ok else int(r.headers.get("content-length",0))
    except: pass
    if ok and total > CHUNK_BYTES*2 and PARALLEL_DL > 1: _dl_parallel(vp, path, total)
    else: _dl_single(vp, path)

_pf = {}
def start_pf(idx, vp):
    path = os.path.join(tempfile.gettempdir(), f"_pf_{idx}.mp4")
    try: os.remove(path)
    except: pass
    evt=threading.Event(); rec={"evt":evt,"path":path,"err":None}; _pf[idx]=rec
    def _t():
        try: dl(vp,path)
        except Exception as e: rec["err"]=e
        finally: evt.set()
    threading.Thread(target=_t, daemon=True).start()
def take_pf(idx):
    rec=_pf.pop(idx, None); if not rec: return None
    rec["evt"].wait(); return None if rec["err"] else rec["path"]

# 终极僵尸文件清理
def _cleanup_zombie_files():
    for f in glob.glob(os.path.join(tempfile.gettempdir(), "_pf_*.mp4")):
        try: os.remove(f)
        except: pass
    for f in glob.glob(os.path.join(tempfile.gettempdir(), "c_*.wav")):
        try: os.remove(f)
        except: pass
atexit.register(_cleanup_zombie_files)

def _chat_mimo(messages):
    body = {
        "model": MIMO_MODEL, 
        "messages": messages, 
        "stream": False, 
        "max_completion_tokens": 16384 if THINKING else 8192, 
        "thinking": {"type": "enabled"} if THINKING else {"type": "disabled"}
    }
    r = post_retry(f"{MIMO_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {MIMO_API_KEY}", "Content-Type": "application/json"}, json=body, timeout=900)
    if add_cost(r.json().get("usage", {})): raise LimitExceeded("停")
    content = r.json()["choices"][0]["message"]["content"]
    if isinstance(content, list): content = "".join(t.get("text","") for t in content if isinstance(t,dict))
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()

def _chat_deepseek(messages):
    for extra in [{"thinking":{"type":"disabled"},"temperature":0.6}, {"temperature":0.6}, {}]:
        body = {"model": DEEP_MODEL, "messages": messages}; body.update(extra)
        try:
            r = post_retry("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}, json=body, timeout=300)
            if add_cost(r.json().get("usage", {})): raise LimitExceeded("停")
            return r.json()["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400: continue
            raise
    raise RuntimeError("DS失败")

def _chat(messages):
    if _stop.is_set(): raise LimitExceeded("停")
    last=None
    if MIMO_API_KEY:
        try: return _chat_mimo(messages)
        except Exception as e: last=e
    if DEEPSEEK_KEY:
        try: return _chat_deepseek(messages)
        except Exception as e: last=e
    raise RuntimeError(f"翻译失败: {last}")

def _parse_batch(txt, n):
    txt = re.sub(r"^\s*```[a-zA-Z]*\s*", "", txt); txt = re.sub(r"\s*```\s*$", "", txt); res={}
    for ln in txt.splitlines():
        m=re.match(r"\s*\[(\d+)\]\s*(.*)",ln)
        if m: res[int(m.group(1))]=m.group(2).strip()
    return [res.get(i+1) for i in range(n)]

def tr_batch(texts, ctx=""):
    body="\n".join(f"[{i+1}] {x}" for i,x in enumerate(texts)); user=(ctx+body) if ctx else body
    with _lock: g=_CUR_GLOSS
    txt=_chat([{"role":"system","content":SYS},{"role":"user","content":g+user if g else user}])
    return _parse_batch(txt, len(texts))

def tr_one(x):
    with _lock: g=_CUR_GLOSS
    return _chat([{"role":"system","content":SYS1},{"role":"user","content":g+x if g else x}]).strip()

def extract_glossary(alls):
    if not (GLOSSARY and alls): return ""
    text=" ".join(s["text"] for s in alls)
    if len(text)>60000: text=text[:60000]
    try: out=_chat([{"role":"system","content":"你是术语提取器。只输出名词表，不翻译整段。"},{"role":"user","content":"从以下英文提取人名/专有名词，给中文译法，每行 英文=中文，无则只输出 NONE：\n"+text}])
    except: return ""
    lines=[ln.strip() for ln in out.splitlines() if "=" in ln and ln.strip().upper()!="NONE"]
    g="\n".join(lines[:80])
    if g: print(f"  专名表 {len(lines)} 条")
    return g

def _run_batches(segs, worker, label):
    n=len(segs); res=[""]*n; batches=list(range(0,n,BATCH))
    if not batches: return res
    def ctx_for(start):
        if start==0: return ""
        lo=max(0,start-BATCH); en=" ".join(s["text"] for s in segs[lo:start]); zh=" / ".join(x for x in res[lo:start] if x)
        head="【前文英文, 仅供语境连贯参考】\n"+en+"\n"
        if zh: head+="【前文中文译文参考】\n"+zh+"\n"
        return head+"【待译字幕】\n"
    def job(start):
        if _stop.is_set(): return
        try: worker(start, ctx_for(start), segs[start:start+BATCH], res)
        except LimitExceeded: pass
    job(batches[0])
    if len(batches) > 1 and not _stop.is_set():
        time.sleep(1.5)
        with ThreadPoolExecutor(max_workers=TR_W) as ex:
            futs=[ex.submit(job,b) for b in batches[1:]]
            for f in futs:
                try: f.result()
                except LimitExceeded: pass
    print(f"    {label} {sum(1 for x in res if x)}/{n}")
    return res

def _do_translate(start, ctx, segs_block, res):
    part=None; texts = [s['text'] for s in segs_block]
    try: part=tr_batch(texts, ctx)
    except Exception: pass
    if part is None: part=[None]*len(texts)
    for j,t in enumerate(texts):
        if _stop.is_set(): part[j]=part[j] or t; continue
        if part[j] is None:
            try: part[j]=tr_one(t)
            except Exception: part[j]=t
    for j,t in enumerate(part): res[start+j]=t

def _do_refine(start, ctx, segs_block, res):
    init=res[start:start+len(segs_block)]
    body="\n".join(f"[{i+1}] {segs_block[i]['text']} / {init[i]}" for i in range(len(segs_block)))
    with _lock: g=_CUR_GLOSS
    user_content = g + body if g else body
    try:
        pr=_parse_batch(_chat([{"role":"system","content":SYS_REFINE},{"role":"user","content":user_content}]), len(segs_block))
        for j,t in enumerate(pr):
            if t: res[start+j]=t
    except Exception: pass

def translate_all(segs):
    res=_run_batches(segs, _do_translate, "翻译")
    if REFINE and not _stop.is_set(): res=_run_batches(segs, _do_refine, "润色")
    return res

def upload_srt(srt_local, srt_rel):
    with open(srt_local,"rb") as f: data=f.read()
    req_retry("PUT", wurl(srt_rel), data=data, timeout=120).raise_for_status()
    if UPLOAD_VERIFY:
        try:
            r = webdav_sess.get(wurl(srt_rel), stream=True, timeout=60)
            r.raise_for_status()
            remote = int(r.headers.get("content-length",0)); r.close()
            if remote != len(data):
                webdav_sess.delete(wurl(srt_rel), timeout=60)
                raise RuntimeError(f"上传校验失败(远端{remote}≠本地{len(data)}), 已删残件")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"上传校验失败: {e}")

def process_local(local, vp, srt_rel):
    global _CUR_GLOSS
    name=os.path.splitext(os.path.basename(vp))[0]
    srt_local = os.path.join(tempfile.gettempdir(), f"{name}.srt")
    tmp=tempfile.mkdtemp()
    try:
        print("  抽音频+切片(噪声门消除幻觉+4分钟大吞吐WAV)...")
        dur = probe_dur(local)
        af = "afftdn=nf=-25,highpass=f=120,lowpass=f=7500,agate=threshold=-32dB:ratio=4:attack=5:release=300,loudnorm=I=-16:TP=-1.5:LRA=11" if AUDIO_ENHANCE else "anull"
        chunks=[]; offsets=[]; durs=[]
        if dur>0:
            n = int(dur//SEG) + (1 if dur%SEG>1 else 0)
            for i in range(n):
                ov = OVERLAP if i>0 else 0; st = max(0.0, i*SEG - ov); ln = SEG + ov
                cp = os.path.join(tmp,f"c_{i:03d}.wav")
                subprocess.run(["ffmpeg","-y","-ss",str(st),"-i",local,"-t",str(ln),"-vn","-ac","1","-ar","16000","-af",af,"-c:a","pcm_s16le",cp], check=True, capture_output=True)
                chunks.append(cp); offsets.append(st); durs.append(ln)
        else:
            subprocess.run(["ffmpeg","-y","-i",local,"-vn","-ac","1","-ar","16000","-f","segment","-segment_time",str(SEG),"-af",af,"-c:a","pcm_s16le",os.path.join(tmp,"c_%03d.wav")], check=True, capture_output=True)
            chunks=sorted(glob.glob(os.path.join(tmp,"c_*.wav")))
            offsets=[i*SEG for i in range(len(chunks))]; durs=[SEG for _ in chunks]
            
        print(f"  共{len(chunks)}片, 开启多线程并发转写[ASR={ASR}]...")
        
        # 🚀 核心大招：利用线程池将所有切片并发甩给 ASR 接口，瞬间榨干网络性能
        def process_chunk(idx, c):
            if _stop.is_set(): return idx, []
            try:
                print(f"  并发转写片段 {idx+1}/{len(chunks)}...")
                segs = transcribe(c, offsets[idx], durs[idx], "")
                return idx, segs
            except Exception as e:
                print(f"  ⚠ 片段 {idx+1} 转写失败: {e}")
                return idx, []

        chunk_results = [[] for _ in chunks]
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
            futs = [ex.submit(process_chunk, i, c) for i, c in enumerate(chunks)]
            for fut in futs:
                idx, segs = fut.result()
                chunk_results[idx] = segs

        alls = []
        for segs in chunk_results:
            alls = merge_segs(alls, segs)

        alls = polish_timing(alls)
        print(f"  转写完成，有效总句数: {len(alls)} 句")
        
        g = extract_glossary(alls)
        with _lock: _CUR_GLOSS = ("【统一译名表】\n"+g+"\n") if g else ""
        
        print(f"  本地化翻译润色(并发={TR_W}, 深度思考={THINKING})...")
        trs = translate_all(alls) if alls else []

        if _stop.is_set() or len(trs) != len(alls): raise LimitExceeded("中断或不匹配,放弃写盘")

        with open(srt_local,"w",encoding="utf-8") as f:
            for n,(s,t) in enumerate(zip(alls,trs),1):
                f.write(f"{n}\n{fmt(s['start'])} --> {fmt(s['end'])}\n{wrap_subtitle(sfx_render(t if isinstance(t,str) else s['text']))}\n\n")
        print("  写回123云盘...")
        upload_srt(srt_local, srt_rel)
    finally:
        try: os.remove(local)
        except: pass
        try: os.remove(srt_local)
        except: pass
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    print(f"🚀 物理极限完全体(Whisper并行转写+4分钟大块吞吐+Mimo极致本土化) | ASR={ASR} | 深度思考={THINKING}")
    try: _chat([{"role":"user","content":"reply OK"}])
    except Exception as e: print("❌ API失败:", e); sys.exit(1)

    allf = walk(ROOT)
    videos = [f for f in allf if os.path.splitext(f)[1].lower() in VIDEO_EXT]
    srt_set = set(f for f in allf if f.lower().endswith(".srt"))
    todo = [vp for vp in videos if (os.path.splitext(vp)[0]+".srt") not in srt_set]
    print(f"📊 待处理视频数 {len(todo)}\n")

    done = 0
    for idx,vp in enumerate(todo):
        if _stop.is_set(): break
        srt_rel = os.path.splitext(vp)[0]+".srt"
        print(f"[{idx+1}/{len(todo)}] {vp}")
        local = take_pf(idx) if PREFETCH else None
        if local is None:
            fd, local = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
            try: dl(vp, local)
            except Exception as e:
                note_auth_fail(e); print("  ❌ 下载失败跳过")
                try: os.remove(local)
                except: pass
                continue
        if PREFETCH and idx+1 < len(todo): start_pf(idx+1, todo[idx+1])
        try:
            process_local(local, vp, srt_rel); done+=1; print(f"  ✅ 完成")
        except LimitExceeded: break
        except Exception as e:
            note_auth_fail(e); print("  ❌ 跳过:", e)
