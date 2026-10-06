#!/usr/bin/env python3
"""
简单的 HTTP 文件服务器，用于提供 /mnt/data 目录的文件下载
运行: python3 file_server.py
访问: http://localhost:8080/filename.docx
"""
from __future__ import annotations

import os
import sys
from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler


class FileServerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory='/mnt/data', **kwargs)

    def end_headers(self):
        # 添加 CORS 头
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        # 强制下载而不是在浏览器中打开
        if self.path.endswith(('.docx', '.doc', '.pdf')):
            filename = os.path.basename(self.path)
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        super().end_headers()


def run_server(port=8080):
    server_address = ('', port)
    httpd = HTTPServer(server_address, FileServerHandler)
    print(f"🚀 文件服务器启动在端口 {port}")
    print('📁 服务目录: /mnt/data')
    print(f"🌐 访问地址: http://localhost:{port}/")
    print('按 Ctrl+C 停止服务器')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n服务器已停止')
        sys.exit(0)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(port)
