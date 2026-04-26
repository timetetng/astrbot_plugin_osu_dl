import re
import aiohttp
import asyncio
import os
import zipfile
import tempfile
import shutil
import time
import io
from urllib.parse import unquote, urlparse

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, Image
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "astrbot_plugin_osu_dl",
    "timetetng",
    "自动解析并发送 osu! 谱面，支持任务管理与并发测速",
    "1.0.1",
)
class OsuDownloaderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 注入插件配置
        self.pending_searches = {}
        # 统一管理所有后台下载任务的生命周期
        self.download_tasks = set()
        self.cache_dir = "/AstrBot/data/osu_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        os.chmod(self.cache_dir, 0o777)
        self.cache_ttl = 86400  # 缓存有效期：24小时 (86400秒)

        logger.info("[OsuDl] osu!谱面下载插件加载成功。")

    def _check_and_copy_cache(self, bms_id: str, dest_path: str) -> bool:
        """检查是否存在有效缓存，若存在则复制到目标临时路径"""
        cache_file = os.path.join(self.cache_dir, f"{bms_id}.osz")
        if os.path.exists(cache_file):
            if time.time() - os.path.getmtime(cache_file) < self.cache_ttl:
                try:
                    shutil.copy2(cache_file, dest_path)
                    os.chmod(dest_path, 0o777)
                    logger.info(f"[OsuDl] 🎯 命中本地缓存: {bms_id}.osz")
                    return True
                except Exception as e:
                    logger.error(f"[OsuDl] 读取缓存失败: {e}")
            else:
                try:
                    os.remove(cache_file)  # 清理过期缓存
                    logger.info(f"[OsuDl] 🧹 清理过期缓存: {bms_id}.osz")
                except:
                    pass
        return False

    def _save_to_cache(self, bms_id: str, src_path: str):
        """将下载成功的文件保存到本地缓存目录"""
        cache_file = os.path.join(self.cache_dir, f"{bms_id}.osz")
        try:
            shutil.copy2(src_path, cache_file)
            os.chmod(cache_file, 0o777)
        except Exception as e:
            logger.error(f"[OsuDl] 写入缓存失败: {e}")

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        if not isinstance(event, AiocqhttpMessageEvent):
            return ""
        raw_msg = event.message_obj.raw_message
        msg_type = raw_msg.get("message_type", "unknown")
        user_id = raw_msg.get("user_id", "unknown")
        group_id = raw_msg.get("group_id", "unknown")
        return f"{msg_type}_{group_id}_{user_id}"

    def _start_download_task(self, event: AstrMessageEvent, raw_ids: list):
        """统一封装任务启动逻辑，追踪句柄防止泄露"""
        task = asyncio.create_task(self.process_downloads(event, raw_ids))
        self.download_tasks.add(task)
        # 任务完成后自动从集合中移除，避免内存泄漏
        task.add_done_callback(self.download_tasks.discard)

    # ==========================================
    # 0. 交互式指令搜索与管理
    # ==========================================
    @filter.command("osuclear")
    async def osu_clear_cmd(self, event: AstrMessageEvent):
        """强制清理所有后台 Osu 下载任务与等待队列"""
        event.stop_event()
        tasks_count = len(self.download_tasks)
        for task in self.download_tasks:
            if not task.done():
                task.cancel()
        self.download_tasks.clear()

        searches_count = len(self.pending_searches)
        self.pending_searches.clear()

        logger.info(
            f"[OsuDl] 触发手动清理，已终止 {tasks_count} 个任务，清除 {searches_count} 个会话。"
        )
        await self._send_napcat_msg(
            event,
            f"🧹 清理完毕！已强制终止 {tasks_count} 个后台下载任务，并清除了 {searches_count} 个待确认的搜索会话。",
        )

    @filter.command("osu")
    async def osu_cmd(self, event: AstrMessageEvent, keyword: str):
        event.stop_event()
        if not keyword:
            await self._send_napcat_msg(
                event,
                "⚠️ 请输入谱面 ID 或歌曲名称。例如：/osu 5526026 或 /osu galaxy\n(如需清理卡死的任务，请使用 /osuclear)",
            )
            return

        if keyword.isdigit():
            self._start_download_task(event, [keyword])
            return

        async with aiohttp.ClientSession() as session:
            url = f"https://catboy.best/api/v2/search?q={keyword}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data:
                            await self._send_napcat_msg(
                                event, f"⚠️ 未找到与“{keyword}”相关的谱面。"
                            )
                            return

                        if len(data) == 1:
                            bms_id = data[0].get("id")
                            title = data[0].get("title")
                            await self._send_napcat_msg(
                                event, f"🔍 找到唯一匹配谱面: {title}，自动开始下载..."
                            )
                            self._start_download_task(event, [str(bms_id)])
                            return

                        results_msg = f"🔍 找到多个匹配谱面，请在 60 秒内输入【序号】选择（发送 0 取消）：\n"
                        search_list = []
                        for idx, item in enumerate(data[:8], 1):
                            bms_id = item.get("id")
                            title = item.get("title")
                            artist = item.get("artist")
                            creator = item.get("creator")
                            search_list.append(str(bms_id))
                            results_msg += (
                                f"{idx}. {artist} - {title} (谱师: {creator})\n"
                            )

                        session_id = self._get_session_id(event)
                        if session_id:
                            self.pending_searches[session_id] = {
                                "list": search_list,
                                "time": time.time(),
                            }
                        await self._send_napcat_msg(event, results_msg.strip())
                    else:
                        await self._send_napcat_msg(
                            event, f"⚠️ 搜索 API 返回错误，状态码: {resp.status}"
                        )
            except Exception as e:
                logger.error(f"[OsuDl] 搜索异常: {e}")
                await self._send_napcat_msg(event, "⚠️ 搜索请求失败，请稍后再试。")

    @filter.regex(r"^\d+$")
    async def osu_search_selection(self, event: AstrMessageEvent):
        session_id = self._get_session_id(event)
        if not session_id or session_id not in self.pending_searches:
            return

        event.stop_event()  # 确认是选择序号，拦截事件
        pending_data = self.pending_searches[session_id]
        if time.time() - pending_data["time"] > 60:
            del self.pending_searches[session_id]
            return

        idx = int(event.message_str.strip())
        if idx == 0:
            await self._send_napcat_msg(event, "✅ 已取消谱面选择。")
            del self.pending_searches[session_id]
            return

        bms_list = pending_data["list"]
        if 1 <= idx <= len(bms_list):
            bms_id = bms_list[idx - 1]
            await self._send_napcat_msg(
                event,
                f"✅ 已确认选择序号 {idx} (解析集ID: {bms_id})，开始后台测速下载...",
            )
            self._start_download_task(event, [bms_id])
            del self.pending_searches[session_id]
        else:
            await self._send_napcat_msg(
                event,
                f"⚠️ 序号无效，请输入 1-{len(bms_list)} 之间的数字，或输入 0 取消。",
            )

    # ==========================================
    # 1.5 难度分析
    # ==========================================
    @filter.command("osu分析")
    async def osu_analyze_cmd(self, event: AstrMessageEvent):
        event.stop_event()
        api_url = self.config.get("analysis_api_url", "").strip()
        if not api_url:
            await self._send_napcat_msg(
                event,
                "⚠️ 难度分析功能未启用，请在配置中填写 analysis_api_url 参数。",
            )
            return

        raw = event.message_str.strip()

        match = re.match(r"^!?\s*osu分析\s+(.+)$", raw, re.IGNORECASE)
        if match:
            keyword = match.group(1).strip()
        else:
            keyword = raw.strip()

        if not keyword:
            await self._send_napcat_msg(
                event,
                "⚠️ 请提供谱面 ID、难度 ID 或谱面链接。\n"
                "格式：!osu分析 <ID或链接> [Mods] [算法]\n"
                "示例：!osu分析 5536219 DT HR\n"
                "支持的 Mod：DT NC HT HR EZ IN HO\n"
                "支持的算法：Sunny Daniel Azusa Mixed",
            )
            return

        logger.info(f"[OsuDl] 难度分析收到 raw keyword: '{keyword}'")
        mods = []
        algorithm = self.config.get("analysis_default_algorithm", "Mixed").strip()
        include_extras = self.config.get("analysis_include_extras", False)

        parts = keyword.strip().split()
        raw_input = parts[0]
        if len(parts) > 1:
            for part in parts[1:]:
                upper = part.upper()
                if upper in ("DT", "NC", "HT", "HR", "EZ", "IN", "HO"):
                    if upper == "NC":
                        mods.append("DT")
                    else:
                        mods.append(upper)
                elif upper in ("SUNNY", "DANIEL", "AZUSA", "MIXED"):
                    algorithm = upper
                elif upper.startswith("ALGO="):
                    algorithm = upper[5:].strip()

        bms_id = self._extract_beatmapset_id(raw_input)
        if not bms_id:
            await self._send_napcat_msg(event, f"⚠️ 无法从「{raw_input}」中提取谱面 ID。")
            return

        resolved_id = bms_id
        try:
            async with aiohttp.ClientSession() as session:
                resolved_id = await self._resolve_bms_id(session, bms_id)
                if resolved_id != bms_id:
                    logger.info(f"[OsuDl] 难度 ID {bms_id} 已解析为谱面集 ID: {resolved_id}")
                bms_id = resolved_id
                metadata = await self._get_beatmap_metadata(session, bms_id)
        except Exception as e:
            logger.error(f"[OsuDl] ID解析异常: {e}")
            metadata = None

        await self._send_napcat_msg(event, f"🔍 正在获取谱面 {bms_id} 并分析难度，请稍候...")

        try:
            osz_path = await self._download_for_analysis(event, bms_id)
            if not osz_path:
                await self._send_napcat_msg(
                    event, f"⚠️ 无法下载谱面 {bms_id}，请检查 ID 是否正确或网络状况。"
                )
                return

            result = await self._analyze_osz(api_url, osz_path, mods, algorithm, include_extras)
            os.unlink(osz_path)

            if not result:
                await self._send_napcat_msg(event, "⚠️ 分析请求失败，请检查 API 服务是否正常运行。")
                return

            if result.get("error"):
                await self._send_napcat_msg(event, f"⚠️ 分析出错：{result['error']}")
                return

            msg = self._format_analysis_result(result, mods, algorithm, include_extras, metadata)
            await self._send_napcat_msg(event, msg)

        except Exception as e:
            logger.error(f"[OsuDl] 难度分析异常: {e}", exc_info=True)
            await self._send_napcat_msg(event, f"⚠️ 分析过程异常：{str(e)}")

    def _extract_beatmapset_id(self, raw_input: str) -> str:
        if raw_input.isdigit():
            return raw_input

        match = re.search(r"osu\.ppy\.sh/beatmapsets/(\d+)", raw_input)
        if match:
            return match.group(1)

        match = re.search(r"/(\d{6,})", raw_input)
        if match:
            return match.group(1)

        return None

    async def _download_for_analysis(self, event: AstrMessageEvent, bms_id: str) -> str:
        shared_temp_base = "/AstrBot/data/osu_temp"
        os.makedirs(shared_temp_base, exist_ok=True)
        os.chmod(shared_temp_base, 0o777)
        temp_dir = tempfile.mkdtemp(dir=shared_temp_base)
        os.chmod(temp_dir, 0o777)

        target_path = os.path.join(temp_dir, f"{bms_id}.osz")

        if self._check_and_copy_cache(str(bms_id), target_path):
            return target_path

        async with aiohttp.ClientSession() as session:
            download_with_video = self.config.get("download_with_video", False)
            if download_with_video:
                mirrors = [
                    f"https://catboy.best/d/{bms_id}",
                    f"https://dl.sayobot.cn/beatmaps/download/full/{bms_id}",
                    f"https://osu.direct/api/d/{bms_id}",
                ]
            else:
                mirrors = [
                    f"https://catboy.best/d/{bms_id}n",
                    f"https://dl.sayobot.cn/beatmaps/download/novideo/{bms_id}",
                    f"https://osu.direct/api/d/{bms_id}",
                ]

            download_success = False

            if self.config.get("use_official_first", True) and self.config.get("osu_session"):
                download_success, official_path = await self._download_official(
                    session, str(bms_id), temp_dir
                )
                if download_success:
                    self._save_to_cache(str(bms_id), official_path)
                    return official_path

            fastest_url = await self._get_fastest_mirror(session, mirrors, str(bms_id))
            download_success = await self._download_file_with_progress(
                session, fastest_url, target_path, str(bms_id)
            )

            if not download_success:
                for link in mirrors:
                    if link != fastest_url:
                        download_success = await self._download_file_with_progress(
                            session, link, target_path, str(bms_id)
                        )
                        if download_success:
                            break

            if download_success:
                self._save_to_cache(str(bms_id), target_path)
                return target_path

        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    async def _analyze_osz(
        self, api_url: str, osz_path: str, mods: list, algorithm: str, include_extras: bool
    ) -> dict:
        url = f"{api_url.rstrip('/')}/analyze"

        file_content = open(osz_path, "rb").read()

        form = aiohttp.FormData()
        form.add_field("algorithm", algorithm)
        if include_extras:
            form.add_field("includeExtras", "true")
        for mod in mods:
            form.add_field("mods[]", mod)
        form.add_field(
            "file",
            io.BytesIO(file_content),
            filename=os.path.basename(osz_path),
            content_type="application/octet-stream",
        )

        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=120)
            async with session.post(url, data=form, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 400:
                    try:
                        return await resp.json()
                    except:
                        return {"error": "请求参数错误 (400)"}
                elif resp.status == 500:
                    try:
                        return await resp.json()
                    except:
                        return {"error": "服务器内部错误 (500)"}
                else:
                    return {"error": f"HTTP {resp.status}"}

    async def _get_beatmap_metadata(self, session: aiohttp.ClientSession, bms_id: str) -> dict:
        info_url = f"https://api.sayobot.cn/v2/beatmapinfo?0={bms_id}"
        try:
            async with session.get(info_url) as resp:
                if resp.status == 200:
                    info_json = await resp.json(content_type=None)
                    if info_json.get("status") == 0 and info_json.get("data"):
                        data = info_json["data"]
                        title = data.get("titleU") or data.get("title", "未知标题")
                        artist = data.get("artistU") or data.get("artist", "未知艺术家")
                        mapper = data.get("creator", "未知谱师")
                        return {
                            "title": title,
                            "artist": artist,
                            "mapper": mapper,
                            "song_name": f"{artist} - {title}",
                        }
        except:
            pass
        return {"title": "未知", "artist": "未知", "mapper": "未知", "song_name": "未知"}

    def _format_analysis_result(self, result: dict, mods: list, algorithm: str, include_extras: bool = False, metadata: dict = None) -> str:
        if not result.get("success"):
            return f"⚠️ 分析失败：{result.get('error', '未知错误')}"

        r = result.get("result", {})
        star = round(r.get("starRating", 0), 2)
        ln_ratio = r.get("lnRatio", 0)
        columns = r.get("columnCount", "?")
        diff_label = r.get("difficultyLabel", "N/A")
        algo_used = r.get("algorithm", algorithm)
        speed_rate = r.get("speedRate", 1.0)
        od_flag = r.get("odFlag")
        cvt_flag = r.get("cvtFlag")

        meta = metadata or {}
        song_name = meta.get("song_name", "未知")
        mapper = meta.get("mapper", "未知")

        msg = f"📊 难度分析结果\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🎵 {song_name}\n"
        msg += f"👤 谱师: {mapper}\n"
        msg += f"⭐ 难度: {star} ★\n"
        if r.get("patternReport"):
            pr = r["patternReport"]
            category = pr.get("Category", "N/A")
            mode_tag = pr.get("ModeTag", "")
            if mode_tag:
                category = f"{category} ({mode_tag})"
            msg += f"🏷️ 键型: {category}\n"
        msg += f"📝 {diff_label}\n"
        msg += f"🎹 {columns}K\n"
        msg += f"📈 LN: {ln_ratio*100:.1f}%\n"

        flags = []
        if mods:
            flags.append(" ".join(mods))
        if od_flag:
            flags.append(f"OD{od_flag}")
        if cvt_flag:
            flags.append(cvt_flag)
        if speed_rate != 1.0:
            flags.append(f"×{speed_rate}")
        if flags:
            msg += f"⚡ {' | '.join(flags)}\n"

        if include_extras and r.get("interludeStar"):
            msg += f"━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"🎼 Interlude: {round(r['interludeStar'], 2)} ★\n"



        return msg.strip()

    # ==========================================
    # 1. 传统正则触发
    # ==========================================
    @filter.regex(r".*osu\.ppy\.sh/beatmapsets/(\d+).*")
    async def auto_download_osu(self, event: AstrMessageEvent):
        message_str = event.message_str
        matches = re.findall(r"osu\.ppy\.sh/beatmapsets/(\d+)", message_str)
        if not matches:
            return
        
        event.stop_event()  # 拦截事件，防止发送含有链接的内容后触发 LLM
        beatmapset_ids = list(set(matches))
        logger.info(f"[OsuDl] 正则提取到谱面 ID: {beatmapset_ids}")
        self._start_download_task(event, beatmapset_ids)

    # ==========================================
    # 2. LLM 工具定义
    # ==========================================
    @filter.llm_tool(name="search_osu_beatmap")
    async def search_osu_beatmap(
        self,
        event: AstrMessageEvent,
        keyword: str,
        mode: int = 3,
        ranked_only: bool = True,
    ) -> str:
        """搜索 osu! 谱面并返回基本信息。"""
        async with aiohttp.ClientSession() as session:
            url = f"https://catboy.best/api/v2/search?q={keyword}&m={mode}"
            if ranked_only:
                url += "&s=ranked"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        for item in data[:5]:
                            bms_id = item.get("id")
                            title = item.get("title")
                            artist = item.get("artist")
                            creator = item.get("creator")
                            status = item.get("status", "unknown")
                            results.append(
                                f"ID: {bms_id} | 歌曲: {artist} - {title} (谱师: {creator}, 状态: {status})"
                            )
                        if results:
                            mode_name = {
                                0: "std",
                                1: "taiko",
                                2: "catch",
                                3: "mania",
                            }.get(mode, str(mode))
                            status_msg = "Ranked " if ranked_only else ""
                            return (
                                f"找到以下匹配的 {status_msg}{mode_name} 模式谱面:\n"
                                + "\n".join(results)
                                + "\n\n你可以根据这些 ID 继续调用 download_osu_beatmaps 工具为用户下载。"
                            )
                        return "未找到符合相关模式和状态的谱面，可以尝试更换关键词，或者让用户放宽搜索条件（如不限制 Ranked）。"
                    return f"搜索 API 请求失败，状态码: {resp.status}"
            except Exception as e:
                return f"搜索时发生异常: {str(e)}"

    @filter.llm_tool(name="download_osu_beatmaps")
    async def download_osu_beatmaps(
        self, event: AstrMessageEvent, bms_ids: list
    ) -> str:
        """下载指定的 osu! 谱面。可接收一个或多个 ID，如果是多个会自动打包成 zip。"""
        str_ids = [str(i) for i in bms_ids]
        self._start_download_task(event, str_ids)
        return f"已成功将 {len(str_ids)} 个谱面加入批量下载队列，底层插件已接管处理，请直接回复用户文件正在后台打包即可。"

    # ==========================================
    # 3. 核心并发测速与解析下载逻辑
    # ==========================================
    async def _resolve_bms_id(self, session: aiohttp.ClientSession, raw_id: str) -> str:
        raw_id_int = 0
        try:
            raw_id_int = int(raw_id)
        except ValueError:
            pass

        async def _check_bid():
            try:
                async with session.get(f"https://api.nerinyan.moe/b/{raw_id}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bms_id = data.get("beatmapset_id")
                        if bms_id:
                            logger.info(
                                f"[OsuDl] 成功将难度 ID {raw_id} (Neri) 溯源解析为谱面集 ID: {bms_id}"
                            )
                            return str(bms_id)
            except:
                pass

            try:
                async with session.get(
                    f"https://catboy.best/api/v2/b/{raw_id}"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bms_id = data.get("beatmapset_id") or data.get("ParentSetId")
                        if bms_id:
                            logger.info(
                                f"[OsuDl] 成功将难度 ID {raw_id} (Mino) 溯源解析为谱面集 ID: {bms_id}"
                            )
                            return str(bms_id)
            except:
                pass
            return None

        async def _check_sid():
            try:
                async with session.get(
                    f"https://api.sayobot.cn/v2/beatmapinfo?0={raw_id}"
                ) as resp:
                    if resp.status == 200:
                        info_json = await resp.json(content_type=None)
                        if info_json.get("status") == 0 and info_json.get("data"):
                            return raw_id
            except:
                pass
            return None

        if raw_id_int > 3500000:
            logger.info(f"[OsuDl] 检测到大数值 ID {raw_id}，判定为难度 ID，开始溯源...")
            res = await _check_bid()
            if res:
                return res
            res = await _check_sid()
            if res:
                return res
        else:
            res = await _check_sid()
            if res:
                return res
            res = await _check_bid()
            if res:
                return res

        return raw_id

    async def _get_fastest_mirror(
        self, session: aiohttp.ClientSession, mirrors: list, bms_id: str
    ) -> str:
        logger.info(
            f"[OsuDl] 正在对谱面 {bms_id} 的 {len(mirrors)} 个节点进行并发测速 (采样 1.5s)..."
        )

        async def test_speed(url: str):
            try:
                start_time = time.time()
                downloaded = 0
                timeout = aiohttp.ClientTimeout(total=5, connect=2)
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            downloaded += len(chunk)
                            if time.time() - start_time >= 1.5:
                                break

                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        domain = url.split("/")[2]
                        logger.info(
                            f"[OsuDl] 测速完成 [{domain}]: {speed / 1048576:.2f} MB/s"
                        )
                        return url, speed
            except Exception as e:
                logger.debug(f"[OsuDl] 节点 {url} 测速失败: {e}")
            return url, -1

        results = await asyncio.gather(*(test_speed(url) for url in mirrors))
        valid_results = [r for r in results if r[1] >= 0]

        if not valid_results:
            logger.warning(
                f"[OsuDl] 谱面 {bms_id} 的所有节点并发测速均失败，回退至首选默认节点。"
            )
            return mirrors[0]

        valid_results.sort(key=lambda x: x[1], reverse=True)
        fastest_url = valid_results[0][0]
        logger.info(
            f"[OsuDl] 🚀 测速选优结束，选用最优节点: {fastest_url.split('/')[2]}"
        )
        return fastest_url

    async def _download_official(
        self, session: aiohttp.ClientSession, bms_id: str, temp_dir: str
    ) -> tuple[bool, str]:
        """使用官方直链下载，返回 (是否成功, 文件完整路径)"""
        osu_session = self.config.get("osu_session", "").strip()
        proxy_url = self.config.get("proxy", "").strip() or None
        if not osu_session:
            return False, ""

        # 读取是否下载视频的配置
        download_with_video = self.config.get("download_with_video", False)
        if download_with_video:
            download_url = f"https://osu.ppy.sh/beatmapsets/{bms_id}/download"
        else:
            download_url = f"https://osu.ppy.sh/beatmapsets/{bms_id}/download?noVideo=1"

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
            "Connection": "keep-alive",
            "Referer": f"https://osu.ppy.sh/beatmapsets/{bms_id}",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        cookies = {"osu_session": osu_session}

        try:
            logger.info(f"[OsuDl] 尝试使用官方渠道下载谱面 {bms_id}...")
            async with session.get(
                download_url,
                headers=headers,
                cookies=cookies,
                proxy=proxy_url,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[OsuDl] 官方下载失败，HTTP状态码: {resp.status} (可能是Cookie已过期或反爬限制)"
                    )
                    return False, ""

                filename = f"{bms_id}.osz"
                content_disposition = resp.headers.get("Content-Disposition")
                if content_disposition:
                    match_filename = re.search(
                        r'filename="([^"]+)"', content_disposition
                    )
                    if match_filename:
                        raw_name = match_filename.group(1)
                        try:
                            filename = unquote(
                                raw_name.encode("latin1").decode(
                                    "utf-8", errors="ignore"
                                )
                            )
                        except:
                            filename = unquote(raw_name)

                filename = re.sub(r'[\\/*?:"<>|]', "", filename)
                filepath = os.path.join(temp_dir, filename)

                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 1024 * 512
                start_time = time.time()
                last_log_time = start_time

                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        current_time = time.time()
                        if total_size > 0 and current_time - last_log_time >= 3:
                            percent = (downloaded / total_size) * 100
                            speed = downloaded / (current_time - start_time) / 1048576
                            logger.info(
                                f"[OsuDl] ⏬ 官网下载中 {bms_id}: {percent:.1f}% ({downloaded / 1048576:.1f}MB / {total_size / 1048576:.1f}MB) - {speed:.2f} MB/s"
                            )
                            last_log_time = current_time

                total_time = time.time() - start_time
                avg_speed = downloaded / total_time / 1048576 if total_time > 0 else 0
                logger.info(
                    f"[OsuDl] ✅ 官方渠道下载完成! 耗时: {total_time:.1f}s, 平均速度: {avg_speed:.2f} MB/s"
                )
                return True, filepath

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[OsuDl] 官方下载异常: {e}")
            return False, ""

    async def _download_file_with_progress(
        self, session: aiohttp.ClientSession, url: str, file_path: str, bms_id: str
    ) -> bool:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 1024 * 512
                    start_time = time.time()
                    last_log_time = start_time

                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)

                            current_time = time.time()
                            if total_size > 0 and current_time - last_log_time >= 3:
                                percent = (downloaded / total_size) * 100
                                speed = (
                                    downloaded / (current_time - start_time) / 1048576
                                )
                                logger.info(
                                    f"[OsuDl] ⏬ 镜像站下载中 {bms_id}: {percent:.1f}% ({downloaded / 1048576:.1f}MB / {total_size / 1048576:.1f}MB) - {speed:.2f} MB/s"
                                )
                                last_log_time = current_time

                    total_time = time.time() - start_time
                    avg_speed = (
                        downloaded / total_time / 1048576 if total_time > 0 else 0
                    )
                    logger.info(
                        f"[OsuDl] ✅ 谱面 {bms_id} 下载完成! 节点: {url.split('/')[2]} | 耗时: {total_time:.1f}s, 平均速度: {avg_speed:.2f} MB/s"
                    )
                    return True
                else:
                    logger.warning(f"[OsuDl] 节点 {url} 返回状态码: {resp.status}")
                    return False
        except asyncio.CancelledError:
            # 捕获手动强制取消的异常并向上抛出，确保任务干净利落地死掉
            logger.warning(f"[OsuDl] ⚠️ 任务被外部强制取消 (节点: {url})")
            raise
        except Exception as e:
            logger.warning(f"[OsuDl] 节点 {url} 请求异常: {e}")
            return False

    async def process_downloads(self, event: AstrMessageEvent, raw_ids: list):
        if not raw_ids:
            return

        if len(raw_ids) == 1:
            await self._download_single(event, raw_ids[0])
        else:
            await self._download_batch_zip(event, raw_ids)

    async def _download_single(self, event: AstrMessageEvent, raw_id: str):
        try:
            async with aiohttp.ClientSession() as session:
                bms_id = await self._resolve_bms_id(session, str(raw_id))
                try:
                    song_name = f"未知歌曲"
                    mapper_name = "未知"

                    info_url = f"https://api.sayobot.cn/v2/beatmapinfo?0={bms_id}"
                    async with session.get(info_url) as info_resp:
                        if info_resp.status == 200:
                            info_json = await info_resp.json(content_type=None)
                            if info_json.get("status") == 0 and info_json.get("data"):
                                data = info_json["data"]
                                title = data.get("titleU") or data.get(
                                    "title", "未知标题"
                                )
                                artist = data.get("artistU") or data.get(
                                    "artist", "未知艺术家"
                                )
                                mapper_name = data.get("creator", "未知麻婆")
                                song_name = f"{artist} - {title}"

                    safe_song_name = re.sub(r'[\\/*?:"<>|]', "", song_name)
                    cover_url = (
                        f"https://assets.ppy.sh/beatmaps/{bms_id}/covers/cover.jpg"
                    )

                    logger.info(f"[OsuDl] 开始下载谱面 {bms_id}")

                    shared_temp_base = "/AstrBot/data/osu_temp"
                    os.makedirs(shared_temp_base, exist_ok=True)
                    os.chmod(shared_temp_base, 0o777)
                    temp_dir = tempfile.mkdtemp(dir=shared_temp_base)
                    os.chmod(temp_dir, 0o777)

                    download_success = False
                    file_path = None
                    filename = f"{bms_id} {safe_song_name}.osz"
                    target_path = os.path.join(temp_dir, filename)

                    # === 读取是否下载视频的配置，动态构建镜像站链接 ===
                    download_with_video = self.config.get("download_with_video", False)
                    if download_with_video:
                        mirrors = [
                            f"https://catboy.best/d/{bms_id}",
                            f"https://dl.sayobot.cn/beatmaps/download/full/{bms_id}",
                            f"https://osu.direct/api/d/{bms_id}",
                        ]
                    else:
                        mirrors = [
                            f"https://catboy.best/d/{bms_id}n",
                            f"https://dl.sayobot.cn/beatmaps/download/novideo/{bms_id}",
                            f"https://osu.direct/api/d/{bms_id}",
                        ]

                    # === 1. 尝试从本地缓存中获取 ===
                    if self._check_and_copy_cache(str(bms_id), target_path):
                        download_success = True
                        file_path = target_path
                    else:
                        # === 2. 优先使用官方渠道 ===
                        if self.config.get(
                            "use_official_first", True
                        ) and self.config.get("osu_session"):
                            (
                                download_success,
                                official_path,
                            ) = await self._download_official(
                                session, str(bms_id), temp_dir
                            )
                            if download_success:
                                file_path = official_path
                                filename = os.path.basename(file_path)

                        # === 3. 官方失败或未开启，回退测速镜像站 ===
                        if not download_success:
                            file_path = target_path
                            fastest_url = await self._get_fastest_mirror(
                                session, mirrors, str(bms_id)
                            )
                            download_success = await self._download_file_with_progress(
                                session, fastest_url, file_path, str(bms_id)
                            )

                            if not download_success:
                                logger.warning(
                                    f"[OsuDl] 最优镜像节点下载失败，回退尝试其余备用节点..."
                                )
                                for link in mirrors:
                                    if link != fastest_url:
                                        download_success = (
                                            await self._download_file_with_progress(
                                                session, link, file_path, str(bms_id)
                                            )
                                        )
                                        if download_success:
                                            break

                        # === 4. 如果全新下载成功，保存至缓存 ===
                        if download_success and file_path:
                            self._save_to_cache(str(bms_id), file_path)

                    do_analysis = self.config.get("download_with_analysis", False) and self.config.get("analysis_api_url", "").strip()

                    if download_success:
                        os.chmod(file_path, 0o777)
                        logger.info(f"[OsuDl] 准备下发文件至 Napcat: {filename}")

                        await self._upload_via_napcat(event, file_path, filename)

                        if do_analysis:
                            api_url = self.config.get("analysis_api_url", "").strip()
                            algorithm = self.config.get("analysis_default_algorithm", "Mixed").strip()
                            include_extras = self.config.get("analysis_include_extras", False)
                            try:
                                results = await asyncio.gather(
                                    self._analyze_osz(api_url, file_path, ["HT"], algorithm, include_extras),
                                    self._analyze_osz(api_url, file_path, [], algorithm, include_extras),
                                    self._analyze_osz(api_url, file_path, ["DT"], algorithm, include_extras),
                                )
                                ht_result = results[0] if results[0] and not results[0].get("error") else None
                                nom_result = results[1] if results[1] and not results[1].get("error") else None
                                dt_result = results[2] if results[2] and not results[2].get("error") else None

                                if nom_result:
                                    r = nom_result.get("result", {})
                                    star = round(r.get("starRating", 0), 2)
                                    ln_ratio = r.get("lnRatio", 0)
                                    diff_label = r.get("difficultyLabel", "N/A")
                                    pattern = ""
                                    if r.get("patternReport"):
                                        pr = r["patternReport"]
                                        category = pr.get("Category", "N/A")
                                        mode_tag = pr.get("ModeTag", "")
                                        if mode_tag:
                                            category = f"{category} ({mode_tag})"
                                        pattern = category

                                    ht_star = ht_result.get("result", {}).get("starRating", 0) if ht_result else 0
                                    dt_star = dt_result.get("result", {}).get("starRating", 0) if dt_result else 0

                                    msg = f"🔗 获取到谱面信息\n"
                                    msg += f"🎵 {song_name}\n"
                                    msg += f"👤 谱师: {mapper_name}\n"
                                    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
                                    msg += f"⭐ 难度: {round(ht_star, 2)} ★/{star} ★/{round(dt_star, 2)} ★\n"
                                    if pattern:
                                        msg += f"🏷️ 键型: {pattern}"
                                        if ln_ratio > 0:
                                            msg += f" | LN:{ln_ratio*100:.1f}%"
                                        msg += f"\n"
                                    elif ln_ratio > 0:
                                        msg += f"📈 LN: {ln_ratio*100:.1f}%\n"
                                    msg += f"📝 {diff_label}\n"
                                    if include_extras and r.get("interludeStar"):
                                        msg += f"🎼 Interlude: {round(r['interludeStar'], 2)} ★\n"
                                    msg = msg.rstrip() + "\n━━━━━━━━━━━━━━━━━━━━"
                                    await self._send_napcat_msg(event, msg, cover_url)
                            except Exception as e:
                                logger.error(f"[OsuDl] 下载后分析异常: {e}")
                    else:
                        await self._send_napcat_msg(
                            event,
                            f"⚠️ 官网以及所有镜像节点均无法下载该文件 (解析集ID:{bms_id})，请尝试手动下载。",
                        )

                    shutil.rmtree(temp_dir, ignore_errors=True)

                except Exception as e:
                    logger.error(f"[OsuDl] 单个下载发生异常: {e}", exc_info=True)
                    await self._send_napcat_msg(
                        event, "⚠️ 处理单个谱面时遇到异常，请检查网络。"
                    )
        except asyncio.CancelledError:
            pass

    async def _download_batch_zip(self, event: AstrMessageEvent, raw_ids: list):
        initial_msg = f"📦 收到批量请求，正在自动校验映射 ID 并多渠道测速下载 {len(raw_ids)} 个谱面，打包为 ZIP 过程可能需要一会，请稍候...\n"
        await self._send_napcat_msg(event, initial_msg)

        shared_temp_base = "/AstrBot/data/osu_temp"
        os.makedirs(shared_temp_base, exist_ok=True)
        os.chmod(shared_temp_base, 0o777)
        temp_dir = tempfile.mkdtemp(dir=shared_temp_base)
        os.chmod(temp_dir, 0o777)

        zip_filename = f"osu_maps_batch_{len(raw_ids)}.zip"
        zip_filepath = os.path.join(temp_dir, zip_filename)
        downloaded_files = []

        try:
            async with aiohttp.ClientSession() as session:
                for idx, raw_id in enumerate(raw_ids, 1):
                    bms_id = await self._resolve_bms_id(session, str(raw_id))
                    logger.info(
                        f"[OsuDl] 正在下载批量任务 {idx}/{len(raw_ids)} (集ID:{bms_id})"
                    )

                    download_success = False
                    target_path = os.path.join(temp_dir, f"{bms_id}.osz")

                    # === 读取是否下载视频的配置，动态构建镜像站链接 ===
                    download_with_video = self.config.get("download_with_video", False)
                    if download_with_video:
                        mirrors = [
                            f"https://catboy.best/d/{bms_id}",
                            f"https://dl.sayobot.cn/beatmaps/download/full/{bms_id}",
                            f"https://osu.direct/api/d/{bms_id}",
                        ]
                    else:
                        mirrors = [
                            f"https://catboy.best/d/{bms_id}n",
                            f"https://dl.sayobot.cn/beatmaps/download/novideo/{bms_id}",
                            f"https://osu.direct/api/d/{bms_id}",
                        ]

                    # === 1. 尝试从缓存获取 ===
                    if self._check_and_copy_cache(str(bms_id), target_path):
                        download_success = True
                        downloaded_files.append(target_path)
                    else:
                        # === 2. 优先官方渠道 ===
                        if self.config.get(
                            "use_official_first", True
                        ) and self.config.get("osu_session"):
                            (
                                download_success,
                                official_path,
                            ) = await self._download_official(
                                session, str(bms_id), temp_dir
                            )
                            if download_success:
                                downloaded_files.append(official_path)
                                self._save_to_cache(
                                    str(bms_id), official_path
                                )  # 写入缓存

                        # === 3. 官方失败回退镜像站 ===
                        if not download_success:
                            file_path = target_path
                            fastest_url = await self._get_fastest_mirror(
                                session, mirrors, str(bms_id)
                            )
                            download_success = await self._download_file_with_progress(
                                session, fastest_url, file_path, str(bms_id)
                            )

                            if not download_success:
                                for link in mirrors:
                                    if link != fastest_url:
                                        download_success = (
                                            await self._download_file_with_progress(
                                                session, link, file_path, str(bms_id)
                                            )
                                        )
                                        if download_success:
                                            break

                            if download_success:
                                downloaded_files.append(file_path)
                                self._save_to_cache(str(bms_id), file_path)  # 写入缓存
                            else:
                                logger.error(
                                    f"[OsuDl] 批量任务 - 官网和镜像均无法下载谱面集 ID: {bms_id} (原输入:{raw_id})"
                                )

            if not downloaded_files:
                await self._send_napcat_msg(
                    event, "⚠️ 提供的所有谱面均未能成功获取，打包失败。"
                )
                return

            logger.info(f"[OsuDl] 开始将 {len(downloaded_files)} 个文件打包为 ZIP...")
            with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file in downloaded_files:
                    zipf.write(file, os.path.basename(file))

            os.chmod(zip_filepath, 0o777)

            logger.info(
                f"[OsuDl] 打包完成，准备通过 file:// 协议向 Napcat 投递压缩包..."
            )
            await self._send_napcat_msg(
                event,
                f"📦 成功下完其中 {len(downloaded_files)} 个谱面，正在向您发卷 ZIP 压缩包...",
            )
            await self._upload_via_napcat(event, zip_filepath, zip_filename)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[OsuDl] 批量打包异常: {e}")
            await self._send_napcat_msg(event, f"⚠️ 打包过程中出现异常: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ==========================================
    # 4. 底层发送封装
    # ==========================================
    async def _send_napcat_msg(
        self, event: AstrMessageEvent, text: str, image_url: str = None
    ):
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        bot = event.bot
        raw_msg = event.message_obj.raw_message
        msg_type = raw_msg.get("message_type")
        target_id = (
            raw_msg.get("group_id") if msg_type == "group" else raw_msg.get("user_id")
        )
        action = "send_group_msg" if msg_type == "group" else "send_private_msg"
        kwargs = (
            {"group_id": target_id} if msg_type == "group" else {"user_id": target_id}
        )

        message_chain = []
        if image_url:
            message_chain.append({"type": "image", "data": {"file": image_url}})
        message_chain.append({"type": "text", "data": {"text": text}})
        kwargs["message"] = message_chain

        try:
            await bot.call_action(action, **kwargs)
        except Exception as e:
            logger.error(f"[OsuDl] 发送图文消息失败: {e}。将尝试剥离图片仅发送文本。")
            if image_url:
                kwargs["message"] = [{"type": "text", "data": {"text": text}}]
                try:
                    await bot.call_action(action, **kwargs)
                except Exception as retry_err:
                    logger.error(f"[OsuDl] 纯文本模式重试发送依然失败: {retry_err}")

    async def process_downloads(self, event: AstrMessageEvent, raw_ids: list):
        if not raw_ids:
            return

        if len(raw_ids) == 1:
            await self._download_single(event, raw_ids[0])
        else:
            await self._download_batch_zip(event, raw_ids)

    async def _upload_via_napcat(
        self, event: AstrMessageEvent, file_path: str, filename: str
    ):
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        bot = event.bot
        raw_msg = event.message_obj.raw_message
        msg_type = raw_msg.get("message_type")

        abs_path = os.path.abspath(file_path)
        file_param = f"file://{abs_path}"

        try:
            if msg_type == "group":
                group_id = raw_msg.get("group_id")
                await bot.call_action(
                    "upload_group_file",
                    group_id=group_id,
                    file=file_param,
                    name=filename,
                )
                logger.info(
                    f"[OsuDl] 成功通过本地路径 file:// 向群 {group_id} 提交文件: {filename}"
                )
            elif msg_type == "private":
                user_id = raw_msg.get("user_id")
                await bot.call_action(
                    "upload_private_file",
                    user_id=user_id,
                    file=file_param,
                    name=filename,
                )
                logger.info(
                    f"[OsuDl] 成功通过本地路径 file:// 向用户 {user_id} 提交文件: {filename}"
                )
        except Exception as api_err:
            logger.error(f"[OsuDl] 本地文件上传调用失败: {api_err}")
            await self._send_napcat_msg(
                event,
                "⚠️ 极速下发请求失败，请检查机器人的 Docker 路径映射情况或目录读写权限。",
            )
