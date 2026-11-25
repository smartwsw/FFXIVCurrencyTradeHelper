#!/usr/bin/env python3
"""在本地起一个静态服务器打开收益表（默认 http://127.0.0.1:8770/）。

浏览器只会从磁盘加载 data.json 之外的资源，直接双击 index.html 会因为
file:// 的跨域限制读不到数据，所以用这个小脚本。
"""
import functools
import http.server
import os
import socketserver
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "docs")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8770


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # 安静一点
        pass


def main() -> int:
    if not os.path.exists(os.path.join(ROOT, "data", "data.json")):
        print("还没有数据：先运行 python build.py --worlds 神意之地")
        return 1
    with socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(Handler, directory=ROOT)) as httpd:
        url = f"http://127.0.0.1:{PORT}/"
        print(f"FF14 货币兑换收益表 → {url}")
        print("按 Ctrl+C 结束")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
