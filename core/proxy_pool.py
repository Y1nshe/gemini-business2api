"""
内置代理节点池（Chromego + mihomo）

目标：
1) 通过 Chromego 稳定更新源拉取节点列表；
2) 使用 mihomo 在本地开启 mixed-port，供浏览器/HTTP 客户端使用；
3) 通过“静态 base 分 + 动态扣分”的策略在节点间切换，直到 preflight 通过或节点池耗尽；
4) 选择时优先使用 mihomo health-check 判定为 alive 的节点，避免大量不可达节点导致的无意义切换；
5) 在当前节点仍可用（preflight 通过）时尽量保持节点稳定，减少不必要的换 IP 带来的额外风控概率。

说明（默认参数，已基于实验拟合选定）：
- score_threshold = 10
- penalty_risk = 60
- penalty_proxy = 30
- penalty_mail = 5
- penalty_preflight = 100
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml

from core.config import config


logger = logging.getLogger("gemini.proxy_pool")


# ----------------------------
# 常量（尽量少的可调项，默认值来自拟合实验）
# ----------------------------

MIXED_PORT = 7893
CONTROLLER_PORT = 9090
GROUP_NAME = "POOL_ACTIVE"

CHROMEGO_REPO = "bannedbook/fanqiang"
CHROMEGO_SCAN_LIMIT = 30
FETCH_TIMEOUT_SECONDS = 20

PREFLIGHT_URLS = [
    "https://www.gstatic.com/generate_204",
    "https://auth.business.gemini.google/login?continueUrl=https://business.gemini.google/",
]
PREFLIGHT_CONNECT_TIMEOUT_SECONDS = 4
PREFLIGHT_MAX_TIME_SECONDS = 10

SCORE_THRESHOLD = 10.0
REFRESH_IDLE_SECONDS = 21600  # 6h：避免轮询间隔较长时过早“忘记”扣分
PROVIDER_REFRESH_MIN_SECONDS = 21600  # 6h：避免频繁触发 GitHub API 拉取导致限流

PENALTY_PREFLIGHT = 100.0
PENALTY_RISK = 60.0
PENALTY_PROXY = 30.0
PENALTY_MAIL = 5.0


# 静态 base 分：基于 probe-gstatic 数据拟合出的简化 LR（仅用名称标签做特征）。
BASE_BIAS = -1.711
KIND_W = {
    "speednode": 1.874,
    "tagged": 1.102,
    "other": -2.281,
    "garbage": -2.406,
}
LINE_W = {
    "商宽": 1.889,
    "原生/疑似家宽": 0.452,
    "家宽": 0.295,
    "机房": -1.534,
}
RISK_W = {
    "高风险": -1.281,
    "低风险": 0.831,
    "非常安全": 1.552,
}


def _data_dir() -> Path:
    """获取数据目录（与 main.py / ConfigManager 保持一致）"""
    if os.path.exists("/data"):
        return Path("/data")
    return Path("./data")


def classify_failure(error: str) -> Tuple[str, str]:
    """将 automation/provider 错误字符串映射为失败类型

    Args:
        error: 错误字符串

    Returns:
        (kind, detail): kind in {"risk","proxy","mail","other"}
    """
    raw = (error or "").strip()
    low = raw.lower()

    if not raw:
        return "other", "empty"

    # 邮件验证码相关（可恢复性较高，扣分最小）
    if "verification code timeout" in low or "验证码超时" in raw or "otp_timeout" in low:
        return "mail", "otp_timeout"

    # 风控/流程阻断（可恢复性最低，扣分最大）
    risk_markers = [
        "signin-error",
        "let's try something else",
        "we had trouble retrieving the email address",
        "recaptcha challenge",
        "send code button not found",
        "code input not found",
        "verification code submission failed",
        "url parameters not found",
        "cid not found",
    ]
    for m in risk_markers:
        if m in low:
            return "risk", m

    # 代理/网络错误（通常需要换节点）
    proxy_markers = [
        "err_proxy_connection_failed",
        "err_tunnel_connection_failed",
        "proxyerror",
        "cannot connect to proxy",
        "connection refused",
        "connect timeout",
        "read timed out",
        "timed out",
        "ssl",
        "net::err_",
    ]
    for m in proxy_markers:
        if m in low:
            return "proxy", m

    return "other", "unknown"


@dataclass(frozen=True)
class ProxyPoolSettings:
    """节点池配置（仅保留极少量必需项）"""

    enabled: bool
    required: bool
    chromego_ip: int


class ProxyPool:
    """节点池管理器

    说明：
    - 该类是线程安全的（注册/刷新任务可能并行跑在不同 executor 线程里）。
    - 仅负责“给自动化提供可用代理”；不强制接管 API 请求的 httpx proxy。
    """

    def __init__(self, s: ProxyPoolSettings):
        self.s = s
        self._lock = threading.Lock()

        self.root_dir = _data_dir() / "proxy_pool"
        self.providers_dir = self.root_dir / "providers"
        self.state_dir = self.root_dir / "state"
        self.cache_dir = self.state_dir / "cache"

        self.proxy_server = f"http://127.0.0.1:{MIXED_PORT}"
        self.controller_base = f"http://127.0.0.1:{CONTROLLER_PORT}"

        # 动态扣分：只增不减（刷新时清零）
        self._dyn_penalty: Dict[str, float] = {}
        self._last_switch_ts = time.monotonic()

        # mihomo 进程
        self._proc: Optional[subprocess.Popen[str]] = None

    @classmethod
    def from_config(cls) -> "ProxyPool":
        """从全局 config.basic 构建节点池实例"""
        enabled = bool(getattr(config.basic, "proxy_pool_enabled", False))
        required = bool(getattr(config.basic, "proxy_pool_required", True))
        chromego_ip = int(getattr(config.basic, "proxy_pool_chromego_ip", 0) or 0)
        if chromego_ip < 0 or chromego_ip > 6:
            chromego_ip = 0
        return cls(ProxyPoolSettings(enabled=enabled, required=required, chromego_ip=chromego_ip))

    def _tool_root(self) -> Path:
        repo_root = Path(__file__).resolve().parent.parent
        return repo_root / "util" / "proxy_pool"

    def _cgpool(self) -> Path:
        return self._tool_root() / "cgpool.py"

    def _mihomo_bin(self) -> Path:
        # NOTE:
        # - Container often runs as a non-root user (see docker-compose `user:`), so /app/util is read-only.
        # - Put runtime-downloaded binaries under the persistent data dir, so it's writable and survives restarts.
        return self.root_dir / "bin" / "mihomo"

    def _mihomo_install_script(self) -> Path:
        return self._tool_root() / "bin" / "install-mihomo.sh"

    def _mihomo_cfg_template(self) -> Path:
        return self._tool_root() / "mihomo.yaml"

    def _pid_file(self) -> Path:
        return self.state_dir / "mihomo.pid"

    def _log_file(self) -> Path:
        return self.state_dir / "mihomo.log"

    def _headers(self) -> Dict[str, str]:
        # 当前配置不使用 controller secret；保留扩展空间。
        return {"Content-Type": "application/json"}

    def _sigmoid(self, z: float) -> float:
        if z >= 0:
            ez = math.exp(-z)
            return 1.0 / (1.0 + ez)
        ez = math.exp(z)
        return ez / (1.0 + ez)

    def _parse_name(self, name: str) -> Tuple[str, str, str]:
        """解析节点名 -> (kind, line, risk)"""
        if "防范境外势力渗透" in name:
            return ("garbage", "garbage", "garbage")
        if "speednode" in name.lower():
            return ("speednode", "speednode", "unknown")
        parts = name.split("｜")
        if len(parts) >= 3:
            line = (parts[1] or "").strip() or "unknown"
            risk = (parts[2] or "").strip().split("(")[0].strip() or "unknown"
            return ("tagged", line, risk)
        return ("other", "unknown", "unknown")

    def _base_score(self, name: str) -> float:
        """静态 base 分（0..100），只依赖名称标签"""
        kind, line, risk = self._parse_name(name)
        z = BASE_BIAS + float(KIND_W.get(kind, 0.0))
        if kind == "tagged":
            z += float(LINE_W.get(line, 0.0))
            z += float(RISK_W.get(risk, 0.0))
        return 100.0 * self._sigmoid(z)

    def _score(self, name: str) -> float:
        base = self._base_score(name)
        dyn = float(self._dyn_penalty.get(name, 0.0))
        return base - dyn

    def _maybe_refresh_dynamic(self) -> None:
        """长时间无切换则刷新动态分（清零）"""
        idle = time.monotonic() - self._last_switch_ts
        if idle < float(REFRESH_IDLE_SECONDS):
            return
        if not any(v > 0 for v in self._dyn_penalty.values()):
            self._last_switch_ts = time.monotonic()
            return
        for k in list(self._dyn_penalty.keys()):
            self._dyn_penalty[k] = 0.0
        self._last_switch_ts = time.monotonic()
        logger.info("[PROXY_POOL] refresh dynamic scores (idle=%ss)", int(idle))

    def _write_pid(self, pid: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file().write_text(str(pid) + "\n", encoding="utf-8")

    def _is_running_pid(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except Exception:
            return False
        return True

    def _is_mihomo_running(self) -> bool:
        try:
            pid_s = self._pid_file().read_text(encoding="utf-8").strip()
            pid = int(pid_s)
        except Exception:
            return False
        return self._is_running_pid(pid)

    def _ensure_dirs(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.providers_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _run_cgpool_fetch(self, ip: int, out_path: Path, raw_out: Path) -> None:
        cgpool = self._cgpool()
        if not cgpool.exists():
            raise RuntimeError(f"missing cgpool.py: {cgpool}")
        cmd = [
            sys.executable,
            "-u",
            str(cgpool),
            "fetch",
            "--repo",
            CHROMEGO_REPO,
            "--scan-limit",
            str(CHROMEGO_SCAN_LIMIT),
            "--ip",
            str(ip),
            "--timeout",
            str(FETCH_TIMEOUT_SECONDS),
            "--cache-dir",
            str(self.cache_dir),
            "--out",
            str(out_path),
            "--raw-out",
            str(raw_out),
        ]
        logger.info("[PROXY_POOL] %s", " ".join(cmd))
        subprocess.run(cmd, check=True)

    def fetch_provider(self) -> None:
        """抓取 Chromego 节点并生成 provider 文件（支持合并 1..6 + 去重）"""
        self._ensure_dirs()
        out_path = self.providers_dir / "chromego.yaml"

        # Prefer using a fresh cached provider to avoid GitHub API rate limits.
        try:
            if out_path.exists():
                age = time.time() - out_path.stat().st_mtime
                if age >= 0 and age < float(PROVIDER_REFRESH_MIN_SECONDS):
                    logger.info("[PROXY_POOL] provider cache hit: age=%ss, skip fetch", int(age))
                    return
        except Exception:
            # Cache check is best-effort.
            pass

        if int(self.s.chromego_ip) == 0:
            ips = [1, 2, 3, 4, 5, 6]
        else:
            ips = [int(self.s.chromego_ip)]

        try:
            merged: List[dict] = []
            for ip in ips:
                tmp_provider = self.state_dir / f"chromego-ip{ip}-provider.yaml"
                raw_out = self.state_dir / f"chromego-ip{ip}-raw.yaml"
                self._run_cgpool_fetch(ip=ip, out_path=tmp_provider, raw_out=raw_out)
                with open(tmp_provider, "r", encoding="utf-8", errors="replace") as f:
                    data = yaml.safe_load(f) or {}
                proxies = data.get("proxies") if isinstance(data, dict) else None
                if not isinstance(proxies, list):
                    continue
                for p in proxies:
                    if isinstance(p, dict):
                        merged.append(p)
        except Exception as exc:
            # If GitHub API is rate limited, reuse any existing provider to keep the system working.
            if out_path.exists():
                logger.warning("[PROXY_POOL] fetch failed, using cached provider: %s: %s", type(exc).__name__, str(exc)[:200])
                return
            raise

        if not merged:
            raise RuntimeError("no proxies fetched from chromego sources")

        # 去重策略：按“节点配置指纹”去重（忽略 name）
        seen_fp: set[str] = set()
        name_seen: Dict[str, int] = {}
        out: List[dict] = []
        for p in merged:
            cfg = dict(p)
            name = str(cfg.get("name") or "").rstrip()
            if not name:
                continue
            cfg.pop("name", None)
            fp = json.dumps(cfg, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            if fp in seen_fp:
                continue
            seen_fp.add(fp)

            if name in name_seen:
                name_seen[name] += 1
                name = f"{name}__dup{name_seen[name]}"
            else:
                name_seen[name] = 0

            out.append(dict(p, name=name))

        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {"proxies": out},
                f,
                sort_keys=False,
                allow_unicode=False,
                default_flow_style=False,
                width=120,
            )
        logger.info("[PROXY_POOL] provider updated: proxies=%s (merged=%s unique=%s)", len(out), len(merged), len(seen_fp))

    def start_mihomo(self) -> None:
        """启动 mihomo（如已启动则跳过）"""
        self._ensure_dirs()
        if self._is_mihomo_running():
            logger.info("[PROXY_POOL] mihomo already running")
            return

        mihomo = self._mihomo_bin()
        if not mihomo.exists():
            installer = self._mihomo_install_script()
            if not installer.exists():
                raise RuntimeError(f"mihomo not found and install script missing: {installer}")
            logger.info("[PROXY_POOL] mihomo missing, installing via %s", installer)
            mihomo.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            # Let the installer write the binary into a writable path.
            env["MIHOMO_BIN"] = str(mihomo)
            subprocess.run(["bash", str(installer)], check=True, env=env)

        cfg_src = self._mihomo_cfg_template()
        cfg_dst = self.root_dir / "mihomo.yaml"
        if not cfg_dst.exists():
            cfg_dst.write_text(cfg_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

        self._log_file().parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(self._log_file(), "a", encoding="utf-8")

        self._proc = subprocess.Popen(
            [str(mihomo), "-d", str(self.root_dir), "-f", str(cfg_dst)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._write_pid(self._proc.pid)
        time.sleep(0.2)
        if self._proc.poll() is not None:
            raise RuntimeError(f"mihomo exited early (code={self._proc.returncode}), log={self._log_file()}")

        # 等待 controller 就绪
        controller = self.controller_base.rstrip("/") + "/proxies"
        ready = False
        for _ in range(60):
            try:
                r = requests.get(controller, timeout=2, headers=self._headers())
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if ready:
            logger.info("[PROXY_POOL] mihomo ready: %s / %s", self.proxy_server, self.controller_base)
        else:
            logger.warning("[PROXY_POOL] mihomo started but controller not ready yet (continuing)")

    def ensure_started(self) -> None:
        """确保节点池已启动（抓取 provider + 启动 mihomo）"""
        if not self.s.enabled:
            return
        with self._lock:
            try:
                self.fetch_provider()
                self.start_mihomo()
            except Exception as exc:
                logger.error("[PROXY_POOL] init failed: %s: %s", type(exc).__name__, str(exc)[:200])
                if self.s.required:
                    raise

    def _get_proxies(self) -> dict:
        url = self.controller_base.rstrip("/") + "/proxies"
        r = requests.get(url, headers=self._headers(), timeout=5)
        r.raise_for_status()
        obj = r.json()
        if not isinstance(obj, dict):
            raise RuntimeError("unexpected /proxies response")
        return obj

    def _get_group(self) -> Tuple[Optional[str], List[str], Dict[str, Optional[bool]]]:
        """读取当前 select group 状态，并返回候选节点列表及 alive 信息

        Returns:
            (now, candidates, alive_map)
            - now: 当前选中的节点名（可能为 None）
            - candidates: group 中的候选节点名（过滤 DIRECT/REJECT）
            - alive_map: name -> alive (True/False/None)。None 表示缺失或未知。
        """
        proxies_json = self._get_proxies()
        proxies = proxies_json.get("proxies", {})
        if not isinstance(proxies, dict):
            raise RuntimeError("unexpected /proxies format: missing proxies object")
        g = proxies.get(GROUP_NAME)
        if not isinstance(g, dict):
            raise RuntimeError(f"group not found: {GROUP_NAME}")
        now = g.get("now")
        if now is not None and not isinstance(now, str):
            now = None
        all_list = g.get("all", [])
        if not isinstance(all_list, list) or not all(isinstance(x, str) for x in all_list):
            raise RuntimeError(f"unexpected group format for {GROUP_NAME}: missing all[]")
        candidates = [n for n in all_list if n not in {"DIRECT", "REJECT"}]
        alive_map: Dict[str, Optional[bool]] = {}
        for n in candidates:
            alive: Optional[bool] = None
            obj = proxies.get(n)
            if isinstance(obj, dict):
                v = obj.get("alive")
                if v is True or v is False:
                    alive = bool(v)
            alive_map[n] = alive
        return now, candidates, alive_map

    def _select_group(self, name: str) -> None:
        url = self.controller_base.rstrip("/") + f"/proxies/{requests.utils.quote(GROUP_NAME, safe='')}"
        r = requests.put(url, headers=self._headers(), json={"name": name}, timeout=5)
        r.raise_for_status()

    def _preflight(self) -> Tuple[bool, List[str]]:
        """通过当前已选节点探测预检 URL 列表是否可达"""
        details: List[str] = []
        for u in PREFLIGHT_URLS:
            ok, detail = self._probe_once(u)
            details.append(detail)
            if not ok:
                return False, details
        return True, details

    def _probe_once(self, url: str) -> Tuple[bool, str]:
        proxies = {"http": self.proxy_server, "https": self.proxy_server}
        try:
            r = requests.get(
                url,
                timeout=(max(1, PREFLIGHT_CONNECT_TIMEOUT_SECONDS), max(1, PREFLIGHT_MAX_TIME_SECONDS)),
                allow_redirects=True,
                proxies=proxies,
                stream=True,
            )
            status = int(r.status_code)
            r.close()
            if status == 407:
                return False, f"{url}: proxy auth required (407)"
            if 200 <= status < 400:
                return True, f"{url}: {status}"
            return False, f"{url}: {status}"
        except Exception as e:
            return False, f"{url}: {type(e).__name__}: {e}"

    def _apply_penalty(self, name: str, kind: str, detail: str = "") -> None:
        if kind == "preflight":
            p = PENALTY_PREFLIGHT
        elif kind == "risk":
            p = PENALTY_RISK
        elif kind == "proxy":
            p = PENALTY_PROXY
        elif kind == "mail":
            p = PENALTY_MAIL
        else:
            p = PENALTY_RISK

        self._dyn_penalty[name] = float(self._dyn_penalty.get(name, 0.0)) + float(p)
        logger.info("[PROXY_POOL] penalty kind=%s -%s score=%.1f name=%s detail=%s", kind, p, self._score(name), name, detail)

    def ensure_proxy_reachable(self) -> None:
        """按分数选择节点并完成 preflight；失败则扣分并继续切换，直到阈值截断"""
        if not self.s.enabled:
            return
        self.ensure_started()

        with self._lock:
            self._maybe_refresh_dynamic()

            switches = 0
            while True:
                now, candidates, alive_map = self._get_group()
                if not candidates:
                    raise RuntimeError("no proxy candidates in group")

                for n in candidates:
                    self._dyn_penalty.setdefault(n, 0.0)

                # Drop nodes that mihomo already marks as dead, but keep a fallback path
                # when everything is unknown/false (e.g., just booted and health-check not ready yet).
                usable = [n for n in candidates if alive_map.get(n) is not False]
                if usable:
                    candidates = usable

                def _alive_rank(v: Optional[bool]) -> int:
                    # Prefer alive=True over unknown, and both over alive=False (filtered out above).
                    if v is True:
                        return 2
                    return 1

                # Sticky behavior: if current node is still healthy (preflight ok),
                # keep it to reduce unnecessary IP churn.
                if now and now in candidates and self._score(now) >= float(SCORE_THRESHOLD):
                    ok, details = self._preflight()
                    if ok:
                        return
                    logger.warning("[PROXY_POOL] preflight failed: %s", " | ".join(details))
                    self._apply_penalty(now, kind="preflight", detail=details[0] if details else "")
                    time.sleep(0.2)

                best = max(candidates, key=lambda n: (_alive_rank(alive_map.get(n)), self._score(n), n))
                best_score = self._score(best)
                if best_score < float(SCORE_THRESHOLD):
                    raise RuntimeError(f"proxy pool exhausted: best_score={best_score:.1f} < threshold={SCORE_THRESHOLD:g}")

                if best != now:
                    self._select_group(best)
                    if now:
                        logger.info(
                            "[PROXY_POOL] switched %s -> %s (score=%.1f, base=%.1f, penalty=%.1f)",
                            now,
                            best,
                            best_score,
                            self._base_score(best),
                            self._dyn_penalty.get(best, 0.0),
                        )
                    else:
                        logger.info(
                            "[PROXY_POOL] selected %s (score=%.1f, base=%.1f, penalty=%.1f)",
                            best,
                            best_score,
                            self._base_score(best),
                            self._dyn_penalty.get(best, 0.0),
                        )
                    self._last_switch_ts = time.monotonic()
                    switches += 1
                    time.sleep(0.2)
                    now = best

                ok, details = self._preflight()
                if ok:
                    if switches > 0:
                        logger.info("[PROXY_POOL] preflight ok after %s switch(es)", switches)
                    return

                logger.warning("[PROXY_POOL] preflight failed: %s", " | ".join(details))
                if now:
                    self._apply_penalty(now, kind="preflight", detail=details[0] if details else "")
                time.sleep(0.2)

    def on_failure(self, kind: str, detail: str) -> None:
        """记录失败并切换到下一个更优节点（包含 preflight）"""
        if not self.s.enabled:
            return
        self.ensure_started()

        with self._lock:
            now, _, _ = self._get_group()
            if not now:
                self.ensure_proxy_reachable()
                return
            self._apply_penalty(now, kind=kind, detail=detail)

        # 重新选择并做 preflight（不在同一把锁内，避免长时间占用）
        self.ensure_proxy_reachable()
