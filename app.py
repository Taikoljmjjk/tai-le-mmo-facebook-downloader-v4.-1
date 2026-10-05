from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from urllib.parse import urlparse
import yt_dlp, tempfile, os, re, shutil, zipfile, json, requests, html

BASE = Path(__file__).resolve().parent
app = FastAPI(title="TÀI LÊ MMO - Facebook Downloader V4.1")
allowed_origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=["*"] if "*" in allowed_origins else allowed_origins,
                   allow_credentials=False, allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE/"static"), name="static")
ALLOWED = {"facebook.com","www.facebook.com","m.facebook.com","fb.watch","web.facebook.com"}

def validate_url(url):
    try:
        p=urlparse(url.strip()); host=(p.hostname or "").lower()
        if p.scheme not in {"http","https"} or (host not in ALLOWED and not host.endswith(".facebook.com")): raise ValueError()
        return url.strip()
    except: raise HTTPException(400,"Liên kết Facebook không hợp lệ.")

def info_extract(url, flat=False, playlistend=None):
    opts={"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":not flat,"socket_timeout":30}
    if flat:
        opts.update({"extract_flat":"in_playlist","yes_playlist":True})
        if playlistend: opts["playlistend"]=playlistend
    with yt_dlp.YoutubeDL(opts) as y: return y.extract_info(url,download=False)

@app.get("/")
def home(): return FileResponse(BASE/"static"/"index.html")

@app.get("/api/health")
def health(): return {"ok":True,"version":"4.1"}

@app.get("/api/info")
def info(url:str=Query(...,min_length=8,max_length=2000)):
    url=validate_url(url)
    try: d=info_extract(url)
    except Exception as e:
        if "login" in str(e).lower() or "cookies" in str(e).lower():
            raise HTTPException(422,"Video yêu cầu đăng nhập/quyền truy cập. Chỉ hỗ trợ nội dung công khai.")
        raise HTTPException(422,"Không thể phân tích video công khai này.")
    fs=[]; seen=set()
    for f in d.get("formats") or []:
        if f.get("vcodec") in (None,"none") or not f.get("height"): continue
        k=(f.get("height"),f.get("ext"))
        if k in seen: continue
        seen.add(k); fs.append({"format_id":f.get("format_id"),"height":f["height"],"quality":f'{f["height"]}p',
          "ext":f.get("ext") or "mp4","has_audio":f.get("acodec") not in (None,"none"),
          "filesize":f.get("filesize") or f.get("filesize_approx")})
    fs.sort(key=lambda x:x["height"],reverse=True)
    return {"id":d.get("id"),"title":d.get("title") or "Facebook Video","thumbnail":d.get("thumbnail"),
            "duration":d.get("duration"),"uploader":d.get("uploader") or d.get("channel"),"formats":fs[:12]}

def download_one(url,height,tmp):
    selector=f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    opts={"format":selector,"outtmpl":str(tmp/"%(title).70s-%(id)s.%(ext)s"),"merge_output_format":"mp4",
          "noplaylist":True,"quiet":True,"no_warnings":True,"restrictfilenames":True}
    before=set(tmp.glob("*"))
    with yt_dlp.YoutubeDL(opts) as y:
        y.extract_info(url,download=True)
    after=[p for p in tmp.glob("*") if p not in before and p.is_file()]
    if not after: raise RuntimeError("Không tạo được file")
    return max(after,key=lambda p:p.stat().st_mtime)

@app.get("/api/download")
def download(url:str,height:int=1080):
    url=validate_url(url)
    if height<144 or height>4320: raise HTTPException(400,"Chất lượng không hợp lệ.")
    tmp=Path(tempfile.mkdtemp(prefix="fbdown_"))
    try: path=download_one(url,height,tmp)
    except Exception:
        shutil.rmtree(tmp,ignore_errors=True); raise HTTPException(422,"Tải video thất bại.")
    safe=re.sub(r'[^A-Za-z0-9._-]+','_',path.name)[:140]
    def stream():
        try:
            with open(path,"rb") as f:
                while True:
                    c=f.read(1024*1024)
                    if not c: break
                    yield c
        finally: shutil.rmtree(tmp,ignore_errors=True)
    return StreamingResponse(stream(),media_type="application/octet-stream",
        headers={"Content-Disposition":f'attachment; filename="{safe}"'})

UA={"User-Agent":"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36","Accept-Language":"vi-VN,vi;q=0.9,en;q=0.8"}

def resolve_facebook_url(url):
    """Resolve Facebook /share/... links without requiring login."""
    try:
        r=requests.get(url,headers=UA,allow_redirects=True,timeout=15,stream=True)
        final=r.url
        r.close()
        return validate_url(final)
    except Exception:
        return url

def canonical_owner_candidates(url):
    p=urlparse(url); path=p.path.strip('/')
    parts=[x for x in path.split('/') if x]
    reserved={'share','reel','reels','watch','video','videos','story.php','photo','groups','marketplace'}
    out=[]
    # A resolved video often looks like /OWNER/videos/VIDEO_ID
    if len(parts)>=3 and parts[1]=='videos': out.append(parts[0])
    # Direct Page/profile URL
    if parts and parts[0].lower() not in reserved: out.append(parts[0])
    return list(dict.fromkeys(out))

def scrape_video_links(page_url, limit):
    """Best-effort public-page discovery fallback for Facebook pages."""
    found=[]; seen=set()
    try:
        r=requests.get(page_url,headers=UA,timeout=20,allow_redirects=True)
        body=html.unescape(r.text).replace('\\/','/')
        patterns=[
          r'https?://(?:www\\.|m\\.)?facebook\\.com/[^"\\s<>]+?/videos/(\\d+)',
          r'https?://(?:www\\.|m\\.)?facebook\\.com/reel/(\\d+)',
          r'facebook\\.com/watch/\\?v=(\\d+)',
          r'"video_id"\\s*:\\s*"?(\\d{6,})"?',
          r'"videoId"\\s*:\\s*"?(\\d{6,})"?'
        ]
        for pat in patterns:
            for vid in re.findall(pat,body,re.I):
                if vid in seen: continue
                seen.add(vid); found.append(f'https://www.facebook.com/watch/?v={vid}')
                if len(found)>=limit: return found
    except Exception: pass
    return found

@app.get("/api/resolve")
def resolve(url:str=Query(...,min_length=8,max_length=2000)):
    original=validate_url(url); final=resolve_facebook_url(original)
    return {"original":original,"resolved":final,"changed":original.rstrip('/')!=final.rstrip('/')}

@app.get("/api/channel")
def channel(url:str=Query(...,min_length=8,max_length=2000),limit:int=20):
    original=validate_url(url); limit=max(1,min(limit,100)); resolved=resolve_facebook_url(original)
    candidates=[resolved]
    for owner in canonical_owner_candidates(resolved):
        candidates += [f'https://www.facebook.com/{owner}/videos',f'https://www.facebook.com/{owner}/reels']
    candidates=list(dict.fromkeys(candidates))
    out=[]; seen=set(); title='Facebook Page'
    # First try yt-dlp playlist extraction on resolved/direct Page variants.
    for candidate in candidates:
        try:
            d=info_extract(candidate,flat=True,playlistend=limit)
            title=d.get('title') or title
            for e in d.get('entries') or []:
                if not e: continue
                vid=str(e.get('id') or '')
                webpage=e.get('webpage_url') or e.get('url')
                if webpage and not str(webpage).startswith('http') and vid: webpage=f'https://www.facebook.com/watch/?v={vid}'
                if not webpage or (vid and vid in seen): continue
                if vid: seen.add(vid)
                out.append({'id':vid or None,'title':e.get('title') or 'Facebook Video','url':webpage,'thumbnail':e.get('thumbnail'),'duration':e.get('duration')})
                if len(out)>=limit: break
        except Exception: pass
        if len(out)>=limit: break
    # Fallback: inspect public HTML for video IDs. This also supports /share/... after redirect resolution.
    if len(out)<limit:
        for candidate in candidates:
            for webpage in scrape_video_links(candidate,limit-len(out)):
                vid=re.search(r'[?&]v=(\\d+)',webpage)
                vid=vid.group(1) if vid else webpage
                if vid in seen: continue
                seen.add(vid); out.append({'id':vid,'title':'Facebook Video','url':webpage,'thumbnail':None,'duration':None})
                if len(out)>=limit: break
            if len(out)>=limit: break
    if not out:
        raise HTTPException(422,"Đã nhận link Facebook nhưng chưa lấy được danh sách video công khai. Page có thể yêu cầu đăng nhập hoặc Facebook đang chặn truy cập máy chủ.")
    return {'title':title,'count':len(out),'videos':out[:limit],'input_url':original,'resolved_url':resolved}

@app.post("/api/batch-download")
async def batch_download(payload:dict):
    urls=payload.get("urls") or []; height=int(payload.get("height") or 720)
    # Conservative server-side cap for free hosting.
    urls=[validate_url(str(x)) for x in urls[:20]]
    if not urls: raise HTTPException(400,"Chưa chọn video.")
    if height<144 or height>2160: raise HTTPException(400,"Chất lượng không hợp lệ.")
    tmp=Path(tempfile.mkdtemp(prefix="fbbatch_")); successes=[]
    try:
        for i,u in enumerate(urls,1):
            try:
                p=download_one(u,height,tmp)
                target=tmp/f"{i:03d}_{p.name}"
                if p!=target: p.rename(target)
                successes.append(target)
            except Exception: pass
        if not successes: raise RuntimeError()
        zp=tmp/"TAI_LE_MMO_FACEBOOK_BATCH.zip"
        with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED,allowZip64=True) as z:
            for p in successes: z.write(p,p.name)
            z.writestr("README.txt",f"Tải thành công {len(successes)}/{len(urls)} video. TAI LE MMO - CHO DI LA CON MAI.")
        def stream():
            try:
                with open(zp,"rb") as f:
                    while True:
                        c=f.read(1024*1024)
                        if not c: break
                        yield c
            finally: shutil.rmtree(tmp,ignore_errors=True)
        return StreamingResponse(stream(),media_type="application/zip",
            headers={"Content-Disposition":'attachment; filename="TAI_LE_MMO_FACEBOOK_BATCH.zip"',
                     "X-Downloaded-Count":str(len(successes))})
    except Exception:
        shutil.rmtree(tmp,ignore_errors=True)
        raise HTTPException(422,"Không tải được danh sách đã chọn. Hãy giảm số lượng hoặc thử lại.")
