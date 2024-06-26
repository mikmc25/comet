import aiohttp, asyncio, bencodepy, hashlib, re, base64, json, os, RTN, time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from databases import Database

from .utils.logger import logger
from .utils.general import translate, isVideo, bytesToSize

database = Database(f"sqlite:///{os.getenv('DATABASE_PATH', 'database.db')}")

class BestOverallRanking(RTN.BaseRankingModel):
    uhd: int = 100
    fhd: int = 90
    hd: int = 80
    sd: int = 70
    dolby_video: int = 100
    hdr: int = 80
    hdr10: int = 90
    dts_x: int = 100
    dts_hd: int = 80
    dts_hd_ma: int = 90
    atmos: int = 90
    truehd: int = 60
    ddplus: int = 40
    aac: int = 30
    ac3: int = 20
    remux: int = 150
    bluray: int = 120
    webdl: int = 90

settings = RTN.SettingsModel()
ranking_model = BestOverallRanking()
rtn = RTN.RTN(settings=settings, ranking_model=ranking_model)

infoHashPattern = re.compile(r"\b([a-fA-F0-9]{40})\b")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    await database.execute("CREATE TABLE IF NOT EXISTS cache (cacheKey BLOB PRIMARY KEY, timestamp INTEGER, results TEXT)")
    yield
    await database.disconnect()

app = FastAPI(lifespan=lifespan, docs_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates("comet/templates")
app.mount("/static", StaticFiles(directory="comet/templates"), name="static")

@app.get("/")
async def root():
    return RedirectResponse("/configure")

indexers = os.getenv("INDEXER_MANAGER_INDEXERS")
if "," in indexers:
    indexers = indexers.split(",")
else:
    indexers = [indexers]

webConfig = {
    "indexers": indexers,
    "languages": [indexer.replace(" ", "_") for indexer in RTN.patterns.language_code_mapping.keys()],
    "resolutions": ["480p", "720p", "1080p", "1440p", "2160p", "2880p", "4320p"]
}

@app.get("/configure")
@app.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "CUSTOM_HEADER_HTML": os.getenv("CUSTOM_HEADER_HTML", ""), "webConfig": webConfig})

def configChecking(b64config: str):
    try:
        config = json.loads(base64.b64decode(b64config).decode())

        if not isinstance(config["debridService"], str) or config["debridService"] not in ["realdebrid"]:
            return False
        
        if not isinstance(config["debridApiKey"], str):
            return False

        if not isinstance(config["indexers"], list):
            return False

        if not isinstance(config["maxResults"], int) or config["maxResults"] < 0:
            return False
        
        if not isinstance(config["resolutions"], list) or len(config["resolutions"]) == 0:
            return False
        
        if not isinstance(config["languages"], list) or len(config["languages"]) == 0:
            return False

        return config
    except:
        return False

@app.get("/manifest.json")
@app.get("/{b64config}/manifest.json")
async def manifest():
    return {
        "id": "stremio.comet.fast",
        "version": "1.0.0",
        "name": "Comet",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "logo": "https://i.imgur.com/jmVoVMu.jpeg",
        "background": "https://i.imgur.com/WwnXB3k.jpeg",
        "resources": [
            "stream"
        ],
        "types": [
            "movie",
            "series"
        ],
        "idPrefixes": [
            "tt"
        ],
        "catalogs": [],
        "behaviorHints": {
            "configurable": True
        }
    }

async def getIndexerManager(session: aiohttp.ClientSession, indexerManagerType: str, indexers: list, query: str):
    try:
        timeout = aiohttp.ClientTimeout(total=int(os.getenv("INDEXER_MANAGER_TIMEOUT", 30)))
        results = []

        if indexerManagerType == "jackett":
            response = await session.get(f"{os.getenv('INDEXER_MANAGER_URL', 'http://127.0.0.1:9117')}/api/v2.0/indexers/all/results?apikey={os.getenv('INDEXER_MANAGER_API_KEY')}&Query={query}&Tracker[]={'&Tracker[]='.join(indexer for indexer in indexers)}", timeout=timeout)
            response = await response.json()

            for result in response["Results"]:
                results.append(result)
        
        if indexerManagerType == "prowlarr":
            getIndexers = await session.get(f"{os.getenv('INDEXER_MANAGER_URL', 'http://127.0.0.1:9696')}/api/v1/indexer", headers={
                "X-Api-Key": os.getenv("INDEXER_MANAGER_API_KEY")
            })
            getIndexers = await getIndexers.json()

            indexersId = []
            for indexer in getIndexers:
                if indexer["definitionName"] in indexers:
                    indexersId.append(indexer["id"])

            response = await session.get(f"{os.getenv('INDEXER_MANAGER_URL', 'http://127.0.0.1:9696')}/api/v1/search?query={query}&indexerIds={'&indexerIds='.join(str(indexerId) for indexerId in indexersId)}&type=search", headers={
                "X-Api-Key": os.getenv("INDEXER_MANAGER_API_KEY")            
            })
            response = await response.json()

            for result in response:
                results.append(result)

        return results
    except Exception as e:
        logger.warning(f"Exception while getting {indexerManagerType} results for {query} with {indexers}: {e}")

async def getTorrentHash(session: aiohttp.ClientSession, indexerManagerType: str, torrent: dict):
    if "InfoHash" in torrent and torrent["InfoHash"] != None:
        return torrent["InfoHash"]
    
    if "infoHash" in torrent:
        return torrent["infoHash"]

    url = torrent["Link"] if indexerManagerType == "jackett" else torrent["downloadUrl"]

    try:
        timeout = aiohttp.ClientTimeout(total=int(os.getenv("GET_TORRENT_TIMEOUT", 5)))
        response = await session.get(url, allow_redirects=False, timeout=timeout)
        if response.status == 200:
            torrentData = await response.read()
            torrentDict = bencodepy.decode(torrentData)
            info = bencodepy.encode(torrentDict[b"info"])
            hash = hashlib.sha1(info).hexdigest()
        else:
            location = response.headers.get("Location", "")
            if not location:
                return

            match = infoHashPattern.search(location)
            if not match:
                return
            
            hash = match.group(1).upper()

        return hash
    except Exception as e:
        logger.warning(f"Exception while getting torrent info hash for {torrent['indexer'] if 'indexer' in torrent else (torrent['Tracker'] if 'Tracker' in torrent else '')}|{url}: {e}")
        # logger.warning(f"Exception while getting torrent info hash for {jackettIndexerPattern.findall(url)[0]}|{jackettNamePattern.search(url)[0]}: {e}")

@app.get("/stream/{type}/{id}.json")
@app.get("/{b64config}/stream/{type}/{id}.json")
async def stream(request: Request, b64config: str, type: str, id: str):
    config = configChecking(b64config)
    if not config:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Comet config.",
                        "url": "https://comet.fast"
                    }
                ]
            }
    
    async with aiohttp.ClientSession() as session:
        checkDebrid = await session.get("https://api.real-debrid.com/rest/1.0/user", headers={
            "Authorization": f"Bearer {config['debridApiKey']}"
        })
        checkDebrid = await checkDebrid.text()
        if not '"type": "premium"' in checkDebrid:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Real-Debrid account.",
                        "url": "https://comet.fast"
                    }
                ]
            }

        season = None
        episode = None
        if type == "series":
            info = id.split(":")

            id = info[0]
            season = int(info[1])
            episode = int(info[2])

        getMetadata = await session.get(f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json")
        metadata = await getMetadata.json()

        name = metadata["d"][0]["l"]
        name = translate(name)

        cacheKey = hashlib.md5(json.dumps({"debridService": config["debridService"], "name": name, "season": season, "episode": episode, "indexers": config["indexers"], "resolutions": config["resolutions"], "languages": config["languages"]}).encode("utf-8")).hexdigest()
        cached = await database.fetch_one(f"SELECT EXISTS (SELECT 1 FROM cache WHERE cacheKey = '{cacheKey}')")
        if cached[0] != 0:
            logger.info(f"Cache found for {name}")

            timestamp = await database.fetch_one(f"SELECT timestamp FROM cache WHERE cacheKey = '{cacheKey}'")
            if timestamp[0] + int(os.getenv("CACHE_TTL", 86400)) < time.time():
                await database.execute(f"DELETE FROM cache WHERE cacheKey = '{cacheKey}'")

                logger.info(f"Cache expired for {name}")
            else:
                sortedRankedFiles = await database.fetch_one(f"SELECT results FROM cache WHERE cacheKey = '{cacheKey}'")
                sortedRankedFiles = json.loads(sortedRankedFiles[0])
                
                results = []
                for hash in sortedRankedFiles:
                    results.append({
                        "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                        "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {bytesToSize(sortedRankedFiles[hash]['data']['size'])}",
                        "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
                    })

                return {"streams": results}
        else:
            logger.info(f"No cache found for {name} with user configuration")

        indexerManagerType = os.getenv("INDEXER_MANAGER_TYPE", "jackett")

        logger.info(f"Start of {indexerManagerType} search for {name} with indexers {config['indexers']}")

        tasks = []
        tasks.append(getIndexerManager(session, indexerManagerType, config["indexers"], name))
        if type == "series":
            tasks.append(getIndexerManager(session, indexerManagerType, config["indexers"], f"{name} S0{season}E0{episode}"))
        searchResponses = await asyncio.gather(*tasks)

        torrents = []
        for results in searchResponses:
            if results == None:
                continue

            for result in results:
                torrents.append(result)

        logger.info(f"{len(torrents)} torrents found for {name}")

        if len(torrents) == 0:
            return {"streams": []}

        tasks = []
        filtered = 0
        for torrent in torrents:
            parsedTorrent = RTN.parse(torrent["Title"]) if indexerManagerType == "jackett" else RTN.parse(torrent["title"])
            if not "All" in config["resolutions"] and len(parsedTorrent.resolution) > 0 and parsedTorrent.resolution[0] not in config["resolutions"]:
                filtered += 1

                continue
            if not "All" in config["languages"] and not parsedTorrent.is_multi_audio and not any(language in parsedTorrent.language for language in config["languages"]):
                filtered += 1

                continue

            tasks.append(getTorrentHash(session, indexerManagerType, torrent))
    
        torrentHashes = await asyncio.gather(*tasks)
        torrentHashes = list(set([hash for hash in torrentHashes if hash]))

        logger.info(f"{len(torrentHashes)} info hashes found for {name}")
        
        if len(torrentHashes) == 0:
            return {"streams": []}

        getAvailability = await session.get(f"https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{'/'.join(torrentHashes)}", headers={
            "Authorization": f"Bearer {config['debridApiKey']}"
        })

        files = {}

        availability = await getAvailability.json()
        for hash, details in availability.items():
            if not "rd" in details:
                continue

            if type == "series":
                for variants in details["rd"]:
                    for index, file in variants.items():
                        filename = file["filename"].lower()
                        
                        if not isVideo(filename):
                            continue

                        filenameParsed = RTN.parse(file["filename"])
                        if season in filenameParsed.season and episode in filenameParsed.episode:
                            files[hash] = {
                                "index": index,
                                "title": file["filename"],
                                "size": file["filesize"]
                            }

                continue

            for variants in details["rd"]:
                for index, file in variants.items():
                    filename = file["filename"].lower()

                    if not isVideo(filename):
                        continue

                    files[hash] = {
                        "index": index,
                        "title": file["filename"],
                        "size": file["filesize"]
                    }

        rankedFiles = set()
        for hash in files:
            # try:
            rankedFile = rtn.rank(files[hash]["title"], hash) # , remove_trash=True, correct_title=name - removed because it's not working great
            rankedFiles.add(rankedFile)
            # except:
            #     continue
        
        sortedRankedFiles = RTN.sort_torrents(rankedFiles)

        logger.info(f"{len(sortedRankedFiles)} cached files found on Real-Debrid for {name}")

        if len(sortedRankedFiles) == 0:
            return {"streams": []}
        
        sortedRankedFiles = {
            key: (value.model_dump() if isinstance(value, RTN.Torrent) else value)
            for key, value in sortedRankedFiles.items()
        }
        for hash in sortedRankedFiles: # needed for caching
            sortedRankedFiles[hash]["data"]["title"] = files[hash]["title"]
            sortedRankedFiles[hash]["data"]["size"] = files[hash]["size"]
            sortedRankedFiles[hash]["data"]["index"] = files[hash]["index"]
        
        jsonData = json.dumps(sortedRankedFiles).replace("'", "''")
        await database.execute(f"INSERT INTO cache (cacheKey, results, timestamp) VALUES ('{cacheKey}', '{jsonData}', {time.time()})")
        logger.info(f"Results have been cached for {name}")
        
        results = []
        for hash in sortedRankedFiles:
            results.append({
                "name": f"[RD⚡] Comet {sortedRankedFiles[hash]['data']['resolution'][0] if len(sortedRankedFiles[hash]['data']['resolution']) > 0 else 'Unknown'}",
                "title": f"{sortedRankedFiles[hash]['data']['title']}\n💾 {bytesToSize(sortedRankedFiles[hash]['data']['size'])}",
                "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sortedRankedFiles[hash]['data']['index']}"
            })

        return {
            "streams": results
        }
    
async def generateDownloadLink(debridApiKey: str, hash: str, index: str):
    try:
        async with aiohttp.ClientSession() as session:
            checkBlacklisted = await session.get("https://real-debrid.com/vpn")
            checkBlacklisted = await checkBlacklisted.text()

            proxy = None
            if "Your ISP or VPN provider IP address is currently blocked on our website" in checkBlacklisted:
                proxy = os.getenv("DEBRID_PROXY_URL", "http://127.0.0.1:1080")
                
                logger.warning(f"Real-Debrid blacklisted server's IP. Switching to proxy {proxy} for {hash}|{index}")

            addMagnet = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/addMagnet", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "magnet": f"magnet:?xt=urn:btih:{hash}"
            }, proxy=proxy)
            addMagnet = await addMagnet.json()

            getMagnetInfo = await session.get(addMagnet["uri"], headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, proxy=proxy)
            getMagnetInfo = await getMagnetInfo.json()

            selectFile = await session.post(f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{addMagnet['id']}", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "files": index
            }, proxy=proxy)

            getMagnetInfo = await session.get(addMagnet["uri"], headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, proxy=proxy)
            getMagnetInfo = await getMagnetInfo.json()

            unrestrictLink = await session.post(f"https://api.real-debrid.com/rest/1.0/unrestrict/link", headers={
                "Authorization": f"Bearer {debridApiKey}"
            }, data={
                "link": getMagnetInfo["links"][0]
            }, proxy=proxy)
            unrestrictLink = await unrestrictLink.json()

            return unrestrictLink["download"]
    except Exception as e:
        logger.warning(f"Exception while getting download link from Real Debrid for {hash}|{index}: {e}")

        return "https://comet.fast"

@app.head("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    downloadLink = await generateDownloadLink(config["debridApiKey"], hash, index)

    return RedirectResponse(downloadLink, status_code=302)
    
@app.get("/{b64config}/playback/{hash}/{index}")
async def stream(b64config: str, hash: str, index: str):
    config = configChecking(b64config)
    if not config:
        return

    downloadLink = await generateDownloadLink(config["debridApiKey"], hash, index)

    return RedirectResponse(downloadLink, status_code=302)
