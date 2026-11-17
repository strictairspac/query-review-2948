"""本地预览：静态托管 frontend，/api/ 反代后端（等价 nginx.conf 行为）。"""
import http.server,socketserver,urllib.request,urllib.error,os
ROOT='/workspace/frontend';BACK='http://127.0.0.1:18001'

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**k):super().__init__(*a,directory=ROOT,**k)
    def _proxy(self):
        body=self.rfile.read(int(self.headers.get('Content-Length') or 0)) or None
        req=urllib.request.Request(BACK+self.path[len('/api'):],data=body,method=self.command)
        for h in ('Content-Type','Authorization'):
            if self.headers.get(h):req.add_header(h,self.headers[h])
        try:resp=urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:resp=e
        data=resp.read();self.send_response(resp.status)
        for k,v in resp.headers.items():
            if k.lower() not in ('transfer-encoding','connection','content-length'):self.send_header(k,v)
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith('/api/'):return self._proxy()
        if not os.path.exists(os.path.join(ROOT,self.path.lstrip('/'))) and '.' not in self.path.rsplit('/',1)[-1]:
            self.path='/index.html'
        return super().do_GET()
    def do_POST(self):return self._proxy()
    def do_PUT(self):return self._proxy()
    def log_message(self,*a):pass

socketserver.ThreadingTCPServer.allow_reuse_address=True
with socketserver.ThreadingTCPServer(('127.0.0.1',18002),H) as s:s.serve_forever()
