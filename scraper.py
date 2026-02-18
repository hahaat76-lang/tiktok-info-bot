import httpx
import json
import re
import asyncio
from datetime import datetime, timezone

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False


class TikTokScraper:
    """Scrapes TikTok user info directly from tiktok.com and video data via tikwm."""

    TIKWM_API = "https://www.tikwm.com/api"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/16.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }

    async def _scrape_tiktok_page(self, url: str) -> dict | None:
        """Scrape a TikTok page and extract embedded JSON data."""
        try:
            async with httpx.AsyncClient(timeout=20, headers=self.headers, follow_redirects=True) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    return None

                html = response.text
                match = re.search(
                    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                    html,
                    re.DOTALL,
                )
                if not match:
                    return None

                data = json.loads(match.group(1))
                scope = data.get("__DEFAULT_SCOPE__", {})
                user_detail = scope.get("webapp.user-detail", {})
                user_info = user_detail.get("userInfo")

                if not user_info or user_detail.get("statusCode") != 0:
                    return None

                return user_info

        except Exception:
            return None

    async def get_user_by_username(self, username: str) -> dict:
        """Fetch TikTok user details by username."""
        username = username.strip().lstrip("@").strip("/")

        # Handle full URLs
        if "tiktok.com" in username:
            match = re.search(r'tiktok\.com/@([^/?]+)', username)
            if match:
                username = match.group(1)
            else:
                return {"error": True}

        # Remove @ if still present
        username = username.lstrip("@")

        user_info = await self._scrape_tiktok_page(f"https://www.tiktok.com/@{username}")
        if not user_info:
            return {"error": True}

        user = user_info.get("user", {})
        stats = user_info.get("stats", {})
        return self._format_user(user, stats)

    async def get_user_by_id(self, user_id: str) -> dict:
        """Fetch TikTok user details by user ID.
        
        TikTok doesn't have a direct ID lookup page, so we try the tikwm API
        as a fallback, and if that fails, return an error with guidance.
        """
        user_id = user_id.strip()

        # Try tikwm API for ID lookup
        try:
            api_headers = {**self.headers, "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=20, headers=api_headers, follow_redirects=True) as client:
                response = await client.post(
                    f"{self.TIKWM_API}/user/info",
                    data={"user_id": user_id},
                )
                data = response.json()
                if data.get("code") == 0 and data.get("data"):
                    user = data["data"]["user"]
                    stats = data["data"]["stats"]
                    return self._format_user(user, stats)
        except Exception:
            pass

        # If tikwm fails, try scraping tiktok.com with the ID as username (sometimes works)
        user_info = await self._scrape_tiktok_page(f"https://www.tiktok.com/@{user_id}")
        if user_info:
            user = user_info.get("user", {})
            stats = user_info.get("stats", {})
            return self._format_user(user, stats)

        return {"error": True}

    async def get_video_no_watermark(self, url: str) -> dict:
        """Download TikTok video without watermark using multiple methods."""
        url = url.strip()

        # Handle short URLs (vm.tiktok.com, vt.tiktok.com)
        if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
            try:
                async with httpx.AsyncClient(timeout=10, headers=self.headers, follow_redirects=True) as client:
                    response = await client.head(url)
                    url = str(response.url)
            except Exception:
                pass

        # Method 1: yt-dlp (most reliable)
        if HAS_YTDLP:
            try:
                result = await self._ytdlp_download(url)
                if result and not result.get("error"):
                    return result
            except Exception:
                pass

        # Method 2: Direct page scraping
        try:
            async with httpx.AsyncClient(timeout=20, headers=self.headers, follow_redirects=True) as client:
                response = await client.get(url)
                html = response.text
                match = re.search(
                    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                    html,
                    re.DOTALL,
                )
                if match:
                    data = json.loads(match.group(1))
                    scope = data.get("__DEFAULT_SCOPE__", {})
                    item_detail = scope.get("webapp.video-detail", {})
                    item_info = item_detail.get("itemInfo", {}).get("itemStruct", {})

                    video = item_info.get("video", {})
                    download_url = video.get("downloadAddr") or video.get("playAddr")

                    if download_url:
                        return {
                            "error": False,
                            "video_url": download_url,
                            "music_url": item_info.get("music", {}).get("playUrl"),
                            "title": item_info.get("desc", ""),
                            "author": item_info.get("author", {}).get("uniqueId", ""),
                            "duration": video.get("duration", 0),
                            "cover": video.get("cover"),
                        }
        except Exception:
            pass

        # Method 3: tikwm API (fallback)
        try:
            api_headers = {**self.headers, "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=15, headers=api_headers, follow_redirects=True) as client:
                response = await client.post(
                    f"{self.TIKWM_API}/",
                    data={"url": url, "hd": 1},
                )
                data = response.json()

                if data.get("code") == 0 and data.get("data"):
                    video_data = data["data"]
                    return {
                        "error": False,
                        "video_url": video_data.get("hdplay") or video_data.get("play"),
                        "music_url": video_data.get("music"),
                        "title": video_data.get("title", ""),
                        "author": video_data.get("author", {}).get("unique_id", ""),
                        "duration": video_data.get("duration", 0),
                        "cover": video_data.get("cover"),
                    }
        except Exception:
            pass

        return {"error": True}

    async def _ytdlp_download(self, url: str) -> dict | None:
        """Use yt-dlp to extract video info."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "best",
            "noplaylist": True,
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, _extract)
            if info:
                video_url = info.get("url")
                if not video_url and info.get("formats"):
                    for fmt in reversed(info["formats"]):
                        if fmt.get("url"):
                            video_url = fmt["url"]
                            break
                if video_url:
                    return {
                        "error": False,
                        "video_url": video_url,
                        "music_url": None,
                        "title": info.get("title", ""),
                        "author": info.get("uploader", ""),
                        "duration": info.get("duration", 0),
                        "cover": info.get("thumbnail"),
                    }
        except Exception:
            pass
        return None

    def _format_user(self, user: dict, stats: dict) -> dict:
        """Format raw TikTok API data into a clean dict."""
        create_time = user.get("createTime", 0)
        if create_time:
            try:
                created_str = datetime.fromtimestamp(
                    int(create_time), tz=timezone.utc
                ).strftime("%d %b %Y %H:%M UTC")
            except (ValueError, OSError):
                created_str = "N/A"
        else:
            created_str = "N/A"

        # Extract region: try region field, then language, then bioLink hints
        region = user.get("region") or None
        language = user.get("language") or None
        region_display = self._resolve_region(region, language)

        # Extract bio link
        bio_link_data = user.get("bioLink")
        bio_link = ""
        if isinstance(bio_link_data, dict):
            bio_link = bio_link_data.get("link", "")
        elif isinstance(bio_link_data, str):
            bio_link = bio_link_data

        # Friends count
        friends = self._format_number(stats.get("friendCount", 0))

        # Digg (liked videos) count
        digg = self._format_number(stats.get("diggCount", 0))

        return {
            "error": False,
            "username": user.get("uniqueId", "N/A"),
            "nickname": user.get("nickname", "N/A"),
            "user_id": str(user.get("id", "N/A")),
            "bio": user.get("signature", "N/A") or "N/A",
            "verified": user.get("verified", False),
            "private": user.get("privateAccount", False),
            "followers": self._format_number(stats.get("followerCount", 0)),
            "following": self._format_number(stats.get("followingCount", 0)),
            "likes": self._format_number(stats.get("heartCount", 0)),
            "videos": self._format_number(stats.get("videoCount", 0)),
            "friends": friends,
            "digg": digg,
            "created": created_str,
            "region": region_display,
            "language": self._lang_code_to_name(language),
            "bio_link": bio_link or "N/A",
            "profile_pic": user.get("avatarLarger", ""),
            "profile_link": f"https://www.tiktok.com/@{user.get('uniqueId', '')}",
            "raw_user": user,
            "raw_stats": stats,
        }

    @staticmethod
    def _resolve_region(region: str | None, language: str | None) -> str:
        """Resolve region from available data."""
        REGION_MAP = {
            "US": "United States 🇺🇸", "GB": "United Kingdom 🇬🇧",
            "CA": "Canada 🇨🇦", "AU": "Australia 🇦🇺",
            "DE": "Germany 🇩🇪", "FR": "France 🇫🇷",
            "SA": "Saudi Arabia 🇸🇦", "AE": "UAE 🇦🇪",
            "EG": "Egypt 🇪🇬", "KW": "Kuwait 🇰🇼",
            "QA": "Qatar 🇶🇦", "BH": "Bahrain 🇧🇭",
            "OM": "Oman 🇴🇲", "JO": "Jordan 🇯🇴",
            "IQ": "Iraq 🇮🇶", "LB": "Lebanon 🇱🇧",
            "MA": "Morocco 🇲🇦", "DZ": "Algeria 🇩🇿",
            "TN": "Tunisia 🇹🇳", "LY": "Libya 🇱🇾",
            "SD": "Sudan 🇸🇩", "YE": "Yemen 🇾🇪",
            "PS": "Palestine 🇵🇸", "SY": "Syria 🇸🇾",
            "TR": "Turkey 🇹🇷", "IN": "India 🇮🇳",
            "BR": "Brazil 🇧🇷", "MX": "Mexico 🇲🇽",
            "JP": "Japan 🇯🇵", "KR": "South Korea 🇰🇷",
            "ID": "Indonesia 🇮🇩", "PH": "Philippines 🇵🇭",
            "TH": "Thailand 🇹🇭", "VN": "Vietnam 🇻🇳",
            "MY": "Malaysia 🇲🇾", "PK": "Pakistan 🇵🇰",
            "RU": "Russia 🇷🇺", "IT": "Italy 🇮🇹",
            "ES": "Spain 🇪🇸", "NL": "Netherlands 🇳🇱",
            "PL": "Poland 🇵🇱", "SE": "Sweden 🇸🇪",
            "NG": "Nigeria 🇳🇬", "ZA": "South Africa 🇿🇦",
            "CO": "Colombia 🇨🇴", "AR": "Argentina 🇦🇷",
            "CL": "Chile 🇨🇱", "PE": "Peru 🇵🇪",
        }
        if region and region.upper() in REGION_MAP:
            return REGION_MAP[region.upper()]
        if region:
            return region.upper()
        return "N/A"

    @staticmethod
    def _lang_code_to_name(code: str | None) -> str:
        """Convert language code to readable name."""
        if not code:
            return "N/A"
        LANG_MAP = {
            "en": "English 🇺🇸", "ar": "العربية 🇸🇦",
            "fr": "Français 🇫🇷", "de": "Deutsch 🇩🇪",
            "es": "Español 🇪🇸", "pt": "Português 🇧🇷",
            "ja": "日本語 🇯🇵", "ko": "한국어 🇰🇷",
            "zh": "中文 🇨🇳", "hi": "हिन्दी 🇮🇳",
            "tr": "Türkçe 🇹🇷", "ru": "Русский 🇷🇺",
            "id": "Bahasa Indonesia 🇮🇩", "th": "ไทย 🇹🇭",
            "vi": "Tiếng Việt 🇻🇳", "it": "Italiano 🇮🇹",
            "nl": "Nederlands 🇳🇱", "pl": "Polski 🇵🇱",
            "ms": "Bahasa Melayu 🇲🇾", "tl": "Filipino 🇵🇭",
            "ur": "اردو 🇵🇰", "fa": "فارسی 🇮🇷",
        }
        return LANG_MAP.get(code.lower(), code)

    @staticmethod
    def _format_number(num) -> str:
        """Format large numbers with commas."""
        try:
            return f"{int(num):,}"
        except (ValueError, TypeError):
            return str(num)
