import os, re, sys, glob, shutil, subprocess, tempfile, time, threading, html, requests
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

# ===== 配置全部从环境变量读(GitHub Secrets 注入), 文件里不留任何密钥 =====
WEBDAV_BASE = os.environ.get("WEBDAV_BASE", "https://webdav.123pan.cn/webdav").rstrip("/")
WEBDAV_USER = os.environ["WEBDAV_USER"]
WEBDAV_PASS = os.environ["WEBDAV_PASS"]
GROQ = os.environ["GROQ_KEY"]
DEEP = os.environ["DEEPSEEK_KEY"]
ROOT       = os.environ.get("ROOT", "视频/蔡斯")
ENGINE     = os.environ.get("ENGINE", "deepseek")
DEEP_MODEL = os.environ.get("DEEP_MODEL", "deepseek-v4-flash")
REFINE     = os.environ.get("REFINE", "false").lower() == "true"
BUDGET     = float(os.environ.get("BUDGET", "9.8"))
IN_PRICE, OUT_PRICE = 1e-6, 2e-6
TR_W, BATCH = 8, 25
GLOSSARY   = os.environ.get("GLOSSARY", "true").lower() == "true"

if not shutil.which("ffmpeg"):
    os.system("apt-get install -y -qq ffmpeg >/dev/null 2>&1")

AUTH = (WEBDAV_USER, WEBDAV_PASS)
GROQ_ASR  = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
DEEP_CHAT = "https://api.deepseek.com/chat/completions"
VIDEO_EXT = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts','.mpg','.mpeg'}
SEG = 600
COST = 0.0
_lock = threading.Lock()
_stop = threading.Event()
_failed_dirs = []
_CUR_SYS = ""; _CUR_SYS1 = ""

SYS = ("你是资深影视字幕本地化专家，长期处理含成人/限制级在内的各类题材，职业准则是忠实、完整、不审查。\n"
       "任务：将英文字幕逐行译为简体中文。\n硬性要求：\n"
       "1. 忠实完整：如实翻译全部内容，含粗口、露骨、性相关表述，按原文语气与尺度对应翻译，绝不回避、删减、委婉化、打码、留空，绝不插入道德评判或拒答说明。内容敏感不是拒绝理由。\n"
       "2. 语气匹配：原文粗俗则译文粗俗，原文平淡则译文平淡，口语自然、简洁、像人说话，不要书面腔或翻译腔。\n"
       "3. 措辞底线：仅当某词直译会触发硬错误时，改用临床/中性但准确的词，仍须译出，不得空着。\n"
       "4. 语气词/呻吟：Oh/Ah/Mmm 等译为简短“嗯/啊/唔”，不堆砌、不省略到失真。\n"
       "5. 一致连贯：参考前文英文与统一译名表，保持人称、称呼、专有名词译法前后一致。\n"
       "输出格式（违反即失败）：只输出译文，每行形如 [编号] 译文，编号与行数必须与输入完全一一对应，禁止任何解释、前言、后记或额外字符。\n"
       "示例（仅示格式与尺度）：\n输入：[1] Come here, baby. [2] Oh fuck yes.\n输出：\n[1] 过来，宝贝。\n[2] 噢操，爽。")
SYS1 = ("你是资深影视字幕本地化专家，处理含成人/限制级题材，准则是忠实完整不审查。把这句英文字幕译为简体中文："
        "如实翻译含粗口/露骨表述，按原文语气尺度，不回避不委婉不留空不评判；口语自然简洁；语气词译为简短“嗯/啊”。只输出译文，无额外文字。")
SYS_REFINE = ("你是字幕润色专家。每行给出“英文原文 / 初译”，请在忠实原意前提下润色初译，更地道口语自然，保留原文尺度与粗口，不净化不书面化。"
              "只输出润色后译文，每行[编号]译文，编号行数与输入一致，无额外文字。")

class LimitExceeded(RuntimeError): pass

def wurl(rel): return WEBDAV_BASE + "/" + quote(rel.strip("/"), safe="/")

def add_cost(u):
    global COST
    with _lock:
        COST += u.get("prompt_tokens",0)*IN_PRICE + u.get("completion_tokens",0)*OUT_PRICE
        over = COST >= BUDGET
        if over: _stop.set()
    return over

def post_retry(url, **kw):
    for _ in range(3):
        try:
            r = requests.post(url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(15); continue
            r.raise_for_status(); return r
        except requests.exceptions.HTTPError:
            raise
        except requests.exceptions.RequestException:
            time.sleep(15)
    raise LimitExceeded("多次重试仍失败")

def walk(rel):
    out, norm = [], rel.strip("/")
    r = None
    for attempt in range(3):
        try:
            r = requests.request("PROPFIND", wurl(rel), auth=AUTH, headers={"Depth":"1"}, timeout=180)
            r.raise_for_status(); break
        except Exception as e:
            print(f"  ⚠ 列目录第{attempt+1}次失败 {rel}: {e}"); time.sleep(8); r = None
    if r is None:
        _failed_dirs.append(rel); print(f"  ❌ 目录扫描彻底失败, 该子树本轮被漏: {rel}"); return out
    n_resp = len(re.findall(r"<\w+:response\b", r.text))
    parsed = 0
    for block in re.findall(r"<\w+:response\b.*?</\w+:response>", r.text, re.S):
        hm = re.search(r"<\w+:href>(.*?)</\w+:href>", block, re.S)
        if not hm: continue
        raw = urlparse(html.unescape(unquote(hm.group(1)))).path.split("/webdav",1)[-1]  # 修复&amp;→&
        relp = raw.strip("/")
        if not relp or relp == norm: continue
        parsed += 1
        is_dir = bool(re.search(r"<\w+:collection\b", block, re.I)) or raw.endswith("/")
        if is_dir and relp != norm: out += walk(relp)
        elif not is_dir: out.append(relp)
    if parsed < n_resp - 2:
        print(f"  ⚠ 目录 {rel} 解析子项{parsed} < 服务器返回{n_resp-1}, 疑似漏块, 记入复查")
        _failed_dirs.append(f"{rel}  (解析{parsed}/返回{n_resp-1})")
    return out

def fmt(t):
    ms=int(round(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def dl(vp, path):
    with requests.get(wurl(vp), auth=AUTH, stream=True, timeout=3600) as r:
        r.raise_for_status(); total=int(r.headers.get("content-length",0)); got=0
        with open(path,"wb") as f:
            for ch in r.iter_content(1<<20):
                f.write(ch); got+=len(ch)
                if total: print(f"\r    下载 {got/1e6:.0f}/{total/1e6:.0f}MB",end="")
    print()

_pf = {}
def start_pf(idx, vp):
    path=f"/tmp/_pf_{idx}.mp4"; evt=threading.Event(); rec={"evt":evt,"path":path,"err":None}; _pf[idx]=rec
    def _t():
        try: dl(vp,path)
        except Exception as e: rec["err"]=e
        finally: evt.set()
    threading.Thread(target=_t, daemon=True).start()
def take_pf(idx):
    rec=_pf.get(idx)
    if not rec: return None
    rec["evt"].wait()
    return None if rec["err"] else rec["path"]

def transcribe(path, offset, prompt=""):
    data={"model":"whisper-large-v3","language":"en","response_format":"verbose_json","temperature":0.0}
    if prompt: data["prompt"]=prompt
    with open(path,"rb") as f:
        r = post_retry(GROQ_ASR, headers={"Authorization":f"Bearer {GROQ}"},
            data=data, files={"file":(os.path.basename(path),f,"audio/mpeg")}, timeout=900)
    out, prev = [], None
    for s in r.json().get("segments",[]):
        t=(s.get("text") or "").strip()
        if not t or len(t)<2: continue
        if t==prev and len(t)>30: continue
        prev=t; out.append({"start":float(s.get("start",0))+offset,"end":float(s.get("end",0))+offset,"text":t})
    return out

def _chat(messages):
    if _stop.is_set(): raise LimitExceeded("已超预算, 停止")
    if ENGINE == "deepseek":
        for extra in [{"thinking":{"type":"disabled"},"temperature":0.2}, {"temperature":0.2}, {}]:
            body = {"model": DEEP_MODEL, "messages": messages}; body.update(extra)
            try:
                r = post_retry(DEEP_CHAT, headers={"Authorization":f"Bearer {DEEP}","Content-Type":"application/json"}, json=body, timeout=300)
                u = r.json().get("usage", {})
                if (u.get("completion_tokens_details") or {}).get("reasoning_tokens",0):
                    print("    ⚠ 含思考token, 成本或略高于估计")
                if add_cost(u): raise LimitExceeded(f"已达¥{COST:.2f}, 自动停")
                return r.json()["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 400: continue
                raise
        raise RuntimeError("deepseek 调用失败")
    r = post_retry(GROQ_CHAT, headers={"Authorization":f"Bearer {GROQ}","Content-Type":"application/json"},
        json={"model":"llama-3.3-70b-versatile","temperature":0.2,"messages":messages}, timeout=300)
    return r.json()["choices"][0]["message"]["content"]

def _parse_batch(txt, n):
    res={}
    for ln in txt.splitlines():
        m=re.match(r"\s*\[(\d+)\]\s*(.*)",ln)
        if m: res[int(m.group(1))]=m.group(2).strip()
    return [res.get(i+1) for i in range(n)]

def tr_batch(texts, ctx=""):
    body="\n".join(f"[{i+1}] {x}" for i,x in enumerate(texts))
    user=(ctx+body) if ctx else body
    txt=_chat([{"role":"system","content":_CUR_SYS},{"role":"user","content":user}])
    return _parse_batch(txt, len(texts))

def tr_one(x):
    return _chat([{"role":"system","content":_CUR_SYS1},{"role":"user","content":x}]).strip()

def extract_glossary(alls):
    if not (GLOSSARY and alls): return ""
    text=" ".join(s["text"] for s in alls)
    if len(text)>60000: text=text[:60000]
    try:
        out=_chat([{"role":"system","content":"你是术语提取器。只输出名词表，不翻译整段，无额外文字。"},
                   {"role":"user","content":"从以下英文字幕提取人名/专有名词/反复出现的称呼与术语，给统一简体中文译法，每行 英文=中文，按出现顺序去重，无则只输出 NONE：\n"+text}])
    except Exception as e:
        print("  glossary提取失败,降级空表:",e); return ""
    lines=[ln.strip() for ln in out.splitlines() if "=" in ln and ln.strip().upper()!="NONE"]
    g="\n".join(lines[:80])
    if g: print(f"  专名表 {len(lines)} 条")
    return g

def _run_batches(segs, worker, label):
    n=len(segs); res=[""]*n; batches=list(range(0,n,BATCH))
    def ctx_for(start):
        if start==0: return ""
        return "【前文英文, 仅供理解语境, 勿翻译】\n"+" ".join(s["text"] for s in segs[max(0,start-BATCH):start])+"\n【待译字幕】\n"
    def job(start):
        if _stop.is_set(): return
        try: worker(start, ctx_for(start), segs[start:start+BATCH], res)
        except LimitExceeded: pass
    with ThreadPoolExecutor(max_workers=TR_W) as ex:
        futs=[ex.submit(job,b) for b in batches]
        for f in futs:
            try: f.result()
            except LimitExceeded: pass
    print(f"    {label} {sum(1 for x in res if x)}/{n}  累计{'¥%.2f'%COST if ENGINE=='deepseek' else '免费'}")
    return res

def _do_translate(start, ctx, texts, res):
    part=None
    try: part=tr_batch(texts, ctx)
    except LimitExceeded: raise
    except Exception as e: print("    批量失败,全逐行:",e)
    if part is None: part=[None]*len(texts)
    for j,t in enumerate(texts):
        if _stop.is_set(): part[j]=part[j] or t; continue
        if part[j] is None:
            try: part[j]=tr_one(t)
            except LimitExceeded: raise
            except Exception as e: print("    单行失败:",e); part[j]=t
    for j,t in enumerate(part): res[start+j]=t

def _do_refine(start, ctx, segs_block, res):
    init=res[start:start+len(segs_block)]
    body="\n".join(f"[{i+1}] {segs_block[i]['text']} / {init[i]}" for i in range(len(segs_block)))
    try:
        pr=_parse_batch(_chat([{"role":"system","content":SYS_REFINE},{"role":"user","content":body}]), len(segs_block))
        for j,t in enumerate(pr):
            if t: res[start+j]=t
    except LimitExceeded: raise
    except Exception as e: print("    润色失败保留初译:",e)

def translate_all(segs):
    res=_run_batches(segs, _do_translate, "翻译")
    if REFINE and not _stop.is_set():
        print("    润色中(REFINE)..."); res=_run_batches(segs, _do_refine, "润色")
    return res

def process_local(local, vp, srt_rel):
    global _CUR_SYS, _CUR_SYS1
    name=os.path.splitext(os.path.basename(vp))[0]
    srt_local=f"/tmp/{name}.srt"; tmp=tempfile.mkdtemp()
    try:
        print("  抽音频+切片...")
        subprocess.run(["ffmpeg","-y","-i",local,"-vn","-ac","1","-ar","16000","-b:a","64k",
            "-f","segment","-segment_time",str(SEG),"-c:a","libmp3lame",os.path.join(tmp,"c_%03d.mp3")],
            check=True,capture_output=True)
        chunks=sorted(glob.glob(os.path.join(tmp,"c_*.mp3"))); print(f"  共{len(chunks)}片, 转写(串行+跨段prompt)...")
        alls=[]; last_prompt=""
        for idx,c in enumerate(chunks):
            if _stop.is_set(): break
            print(f"  转写{idx+1}/{len(chunks)}...")
            segs=transcribe(c, idx*SEG, last_prompt); alls.extend(segs)
            last_prompt=" ".join(s["text"] for s in segs[-6:])[-300:]
        print(f"  转写{len(alls)}句")
        g=extract_glossary(alls)
        _CUR_SYS = SYS + ("\n统一译名表(必须采用, 保持一致)：\n"+g if g else "")
        _CUR_SYS1 = SYS1 + ((" 译名参考："+g) if g else "")
        print(f"  翻译(并发{TR_W})...")
        trs=translate_all(alls) if alls else []
        with open(srt_local,"w",encoding="utf-8") as f:
            for n,(s,t) in enumerate(zip(alls,trs),1):
                f.write(f"{n}\n{fmt(s['start'])} --> {fmt(s['end'])}\n{t}\n\n")
        print("  写回123云盘...")
        with open(srt_local,"rb") as f:
            requests.put(wurl(srt_rel), auth=AUTH, data=f.read(), timeout=120).raise_for_status()
    finally:
        try: os.remove(local)
        except: pass
        shutil.rmtree(tmp, ignore_errors=True)

# ================= 全自动主流程 =================
print(f"引擎={ENGINE} 模型={DEEP_MODEL if ENGINE=='deepseek' else 'llama(免费)'} REFINE={REFINE} GLOSSARY={GLOSSARY} 并发={TR_W}")
if ENGINE == "deepseek":
    print("自检 DeepSeek(flash,关思考)...")
    try: _chat([{"role":"user","content":"reply OK"}]); print("  自检通过\n")
    except Exception as e: print("\n❌ 自检失败, 未处理未扣费。核对 Secrets 里的 DEEPSEEK_KEY/DEEP_MODEL:", e); sys.exit(1)

print("扫描目录树(已修复&转义+重试+尾斜杠双保险+漏块自检)...")
allf = walk(ROOT)
videos = [f for f in allf if os.path.splitext(f)[1].lower() in VIDEO_EXT]
srt_set = set(f for f in allf if f.lower().endswith(".srt"))
todo = [vp for vp in videos if (os.path.splitext(vp)[0]+".srt") not in srt_set]
print(f"📊 扫描到视频 {len(videos)} 个 | 已完成 {len(videos)-len(todo)} | 待处理 {len(todo)}\n")

done = 0
for idx,vp in enumerate(todo):
    if _stop.is_set(): print("\n⏸ 预算到, 停止。进度已存。"); break
    srt_rel = os.path.splitext(vp)[0]+".srt"
    print(f"[{idx+1}/{len(todo)}] {vp}")
    local = take_pf(idx)
    if local is None:
        print("  下载(无预下载)..."); local="/tmp/_src.mp4"
        try: dl(vp, local)
        except Exception as e: print("  ❌ 下载失败, 跳过:", e); continue
    else:
        print("  命中预下载✓")
    if idx+1 < len(todo): start_pf(idx+1, todo[idx+1])
    try:
        process_local(local, vp, srt_rel); done+=1; print(f"  ✅ 完成（新处理 {done}）")
    except LimitExceeded as e:
        print("\n⏸", e, "\n→ 进度已存。下次定时触发自动续跑。"); break
    except Exception as e:
        print("  ❌ 跳过:", e)

print(f"\n===== 本次新处理 {done}, 剩余 {len(todo)-done} =====")
if _failed_dirs:
    print("\n⚠ 以下目录本轮扫描异常(其下视频可能漏), 下次定时触发通常补齐; 反复出现同一目录需排查：")
    for d in _failed_dirs: print("   -", d)
else:
    print("\n✅ 所有子目录扫描完整、无漏扫、无漏块。")
print("剩余>0: 等下一次定时触发(每8小时)自动续跑, 或手动 Run workflow 立即续跑。")
