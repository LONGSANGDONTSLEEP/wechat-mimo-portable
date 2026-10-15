import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import unittest
import app

class OllamaTests(unittest.TestCase):
    def test_local_chat_transport_and_no_cloud_key(self):
        captured={}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured['path']=self.path; captured['headers']=dict(self.headers)
                captured['body']=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body=json.dumps({'choices':[{'message':{'content':'<think>内部思考</think>本地回复'}}]}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            for provider in ('ollama','lmstudio'):
                cfg=dict(app.DEFAULT,provider=provider,base_url=f'http://127.0.0.1:{server.server_port}/v1',model='local-test:8b')
                self.assertEqual(app.completion(cfg,'sk-fake-cloud-key','中文消息',[]),'本地回复')
                self.assertNotIn('authorization',{k.lower():v for k,v in captured['headers'].items()})
                self.assertNotIn('api-key',{k.lower():v for k,v in captured['headers'].items()})
            self.assertEqual(captured['path'],'/v1/chat/completions')
            self.assertNotIn('api-key',{k.lower():v for k,v in captured['headers'].items()})
            self.assertNotIn('authorization',{k.lower():v for k,v in captured['headers'].items()})
            self.assertEqual(captured['body']['model'],'local-test:8b')
            self.assertIn('max_tokens',captured['body'])
            self.assertNotIn('max_completion_tokens',captured['body'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_ollama_base_without_v1_and_empty_key(self):
        for base in ('http://localhost:11434','http://localhost:11434/api'):
            cfg=dict(app.DEFAULT,provider='ollama',base_url=base,model='local-test:8b')
            key,url=app.validate_connection(cfg,'')
            self.assertEqual(key,'');self.assertEqual(url,'http://localhost:11434/v1/chat/completions')

    def test_cloud_http_still_rejected(self):
        with self.assertRaises(ValueError):app.validate_connection(dict(app.DEFAULT,base_url='http://api.example.com/v1'),'sk-fake-test')

if __name__=='__main__':unittest.main()
