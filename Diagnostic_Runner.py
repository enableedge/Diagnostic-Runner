#!/usr/bin/env python3
import os
import time
import json
import logging
import argparse

from seleniumwire import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from jinja2 import Template

# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><title>Smart Diagnostics Report</title>
  <style>
    body { font-family: Arial; margin: 20px; }
    h2 { border-bottom: 1px solid #ccc; padding-bottom: 4px; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #f4f4f4; }
    .error { color: #c00 }
    .warn  { color: #e65 }
    .ok    { color: #090 }
  </style>
</head>
<body>
  <h1>Smart Diagnostics Report</h1>
  {% for page, data in report.pages.items() %}
    <h2>{{ page }}</h2>

    <h3>Performance</h3>
    <p>
      Load Time: <strong>{{ data.performance.page_load_time_ms }} ms</strong>
      (Expected ≤ {{ report.standard.performance.page_load_ms }} ms)
      {% if data.performance.page_load_time_ms > report.standard.performance.page_load_ms %}
        <span class="error">FAIL</span>
      {% else %}
        <span class="ok">PASS</span>
      {% endif %}
    </p>

    <h3>Console Issues</h3>
    {% for level in ['errors','warnings','deprecations'] %}
      <p>{{ level.title() }}:
        <span class="{{ 'error' if level=='errors' else 'warn' }}">
          {{ data.console_issues[level]|length }}
        </span>
      </p>
      {% if data.console_issues[level] %}
      <table>
        <tr><th>Message</th></tr>
        {% for msg in data.console_issues[level] %}
          <tr><td>{{ msg }}</td></tr>
        {% endfor %}
      </table>
      {% endif %}
    {% endfor %}

    <h3>API Issues</h3>
    <p>
      Errors: <span class="error">{{ data.api_issues.errors|length }}</span>,
      Timeouts: <span class="warn">{{ data.api_issues.timeouts|length }}</span>,
      Slow: <span class="warn">{{ data.api_issues.slow_responses_ms|length }}</span>
    </p>

    <h3>Resource Issues</h3>
    <p>
      Missing/404: <span class="error">{{ data.resource_issues.missing_or_404|length }}</span>,
      Slow: <span class="warn">{{ data.resource_issues.slow_resources_ms|length }}</span>,
      Oversized Images: <span class="error">{{ data.resource_issues.oversized_images|length }}</span>
      (Max {{ report.standard.resource.image_max_kb }} KB)
    </p>
    {% if data.resource_issues.oversized_images %}
      <table>
        <tr><th>URL</th><th>Size (KB)</th><th>Result</th></tr>
        {% for img in data.resource_issues.oversized_images %}
        <tr>
          <td>{{ img.url }}</td>
          <td>{{ img.size_kb }}</td>
          <td>
            {% if img.size_kb > report.standard.resource.image_max_kb %}
              <span class="error">FAIL</span>
            {% else %}
              <span class="ok">PASS</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </table>
    {% endif %}

  {% endfor %}
</body>
</html>
"""
# ─────────────────────────────────────────────────────────────────────────────

def load_urls_from_file(path: str) -> list:
    """
    Load URLs from:
      - Plain text: one URL per line
      - JSON: top-level array of URL strings
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if path.lower().endswith(".json"):
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                return [u.strip() for u in arr if isinstance(u, str) and u.strip()]
        except json.JSONDecodeError:
            pass

    return [line.strip() for line in content.splitlines() if line.strip()]


class SmartDiagnosticsRunner:
    def __init__(
        self,
        headless: bool = True,
        page_load_timeout: int = 20,
        page_load_standard_ms: int = 3000,
        res_slow_th: int = 2000,
        api_slow_th: int = 3000,
        image_size_standard_kb: int = 5,
        log_level: int = logging.INFO
    ):
        logging.basicConfig(
            filename="smartdiag.log",
            level=log_level,
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.logger = logging.getLogger("SmartDiagnostics")

        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # Disable Chrome time‐sync pings
        opts.add_argument(
            "--disable-features=NetworkTimeService,NetworkTimeServiceQuerying"
        )
        opts.add_argument("--disable-background-networking")
        opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

        self.driver = webdriver.Chrome(service=Service(), options=opts)
        self.driver.set_page_load_timeout(page_load_timeout)

        # thresholds & standards
        self.page_load_timeout = page_load_timeout
        self.res_slow_th = res_slow_th
        self.api_slow_th = api_slow_th
        self.page_load_standard_ms = page_load_standard_ms
        self.image_size_standard_kb = image_size_standard_kb

        # report container
        self.report = type("R", (), {})()
        self.report.pages = {}
        self.report.standard = {
            "performance": {"page_load_ms": page_load_standard_ms},
            "resource":    {"image_max_kb": image_size_standard_kb}
        }

    def run(self, urls: list, json_path: str, html_path: str):
        try:
            for url in urls:
                self._process_page(url)
        finally:
            self._write_reports(json_path, html_path)
            self.driver.quit()

    def _process_page(self, url: str):
        self.logger.info("Visiting %s", url)
        self.driver.requests.clear()

        page = {
            "performance": {"page_load_time_ms": 0},
            "console_issues": {"errors": [], "warnings": [], "deprecations": []},
            "api_issues":    {"errors": [], "timeouts": [], "slow_responses_ms": []},
            "resource_issues": {
                "missing_or_404": [], "slow_resources_ms": [], "oversized_images": []
            }
        }

        start = time.time()
        try:
            self.driver.get(url)
        except TimeoutException:
            self.logger.warning(
                "Page load timed out after %ds: %s",
                self.page_load_timeout, url
            )

        # wait up to standard page load time for readyState
        elapsed = time.time() - start
        while elapsed * 1000 < self.page_load_standard_ms:
            if self.driver.execute_script("return document.readyState") == "complete":
                break
            time.sleep(0.1)
            elapsed = time.time() - start

        page["performance"]["page_load_time_ms"] = round(elapsed * 1000)
        self._capture_console(page)
        self._capture_network_and_resources(page)

        self.report.pages[url] = page
        self.logger.info("Finished %s in %d ms", url, page["performance"]["page_load_time_ms"])

    def _capture_console(self, page: dict):
        for entry in self.driver.get_log("browser"):
            msg, lvl = entry["message"], entry["level"].upper()
            if "deprecated" in msg.lower():
                page["console_issues"]["deprecations"].append(msg)
            elif lvl == "SEVERE":
                page["console_issues"]["errors"].append(msg)
            elif lvl == "WARNING":
                page["console_issues"]["warnings"].append(msg)

    def _capture_network_and_resources(self, page: dict):
        try:
            resources = self.driver.execute_script(
                """return performance.getEntriesByType('resource')
                    .map(r => ({
                      name: r.name,
                      type: r.initiatorType,
                      duration: Math.round(r.duration),
                      size: Math.round(r.encodedBodySize)
                    }));"""
            )
        except Exception:
            resources = []

        for r in resources:
            if r["duration"] > self.res_slow_th:
                page["resource_issues"]["slow_resources_ms"].append(r)
            if r["type"] == "img":
                size_kb = round(r["size"] / 1024, 1)
                if size_kb > self.image_size_standard_kb:
                    page["resource_issues"]["oversized_images"].append({
                        "url": r["name"], "size_kb": size_kb
                    })

        for req in self.driver.requests:
            # skip Chrome time‐sync
            if "clients2.google.com/time/1/current" in req.url:
                continue

            if not req.response:
                page["api_issues"]["timeouts"].append({
                    "url": req.url, "method": req.method
                })
                continue

            status = req.response.status_code
            if status >= 400:
                page["api_issues"]["errors"].append({
                    "url": req.url, "status": status, "method": req.method
                })

            start, finish = getattr(req, "date", None), getattr(req.response, "date", None)
            if start and finish:
                ms = (finish - start).total_seconds() * 1000
                if ms > self.api_slow_th:
                    page["api_issues"]["slow_responses_ms"].append({
                        "url": req.url, "duration_ms": round(ms)
                    })

    def _write_reports(self, json_path: str, html_path: str):
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        with open(json_path, "w") as jf:
            json.dump({
                "pages": self.report.pages,
                "standard": self.report.standard
            }, jf, indent=2)

        tpl = Template(HTML_TEMPLATE)
        with open(html_path, "w", encoding="utf-8") as hf:
            hf.write(tpl.render(report=self.report))
        self.logger.info("Reports saved to %s and %s", json_path, html_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run SmartDiagnosticsRunner on URLs or a file of URLs"
    )
    parser.add_argument(
        "-f", "--urls-file",
        help="Path to .txt or .json file listing URLs (one per line or JSON array)"
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more URLs to diagnose (ignored if --urls-file is set)"
    )
    parser.add_argument(
        "--json",
        default="reports/smart_report.json",
        help="Path for JSON report"
    )
    parser.add_argument(
        "--html",
        default="reports/smart_report.html",
        help="Path for HTML report"
    )
    args = parser.parse_args()

    # Auto-detect a single-file argument if -f not used
    if args.urls_file:
        source = args.urls_file
    elif len(args.urls) == 1 and os.path.isfile(args.urls[0]):
        source = args.urls[0]
    else:
        source = None

    if source:
        urls_to_test = load_urls_from_file(source)
    else:
        urls_to_test = args.urls

    if not urls_to_test:
        parser.error("No URLs provided. Pass real URLs or a file of URLs (-f).")

    runner = SmartDiagnosticsRunner(headless=True)
    runner.run(urls_to_test, args.json, args.html)
