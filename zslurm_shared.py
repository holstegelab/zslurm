import sys
if sys.version_info.major == 2:
    import xmlrpclib
    import httplib
else:
    import xmlrpc.client as xmlrpclib
    import http.client as httplib

import socket
from dns import resolver, reversename
import time

#COMMANDS
NOOP=0
STOP=1
DIE=2
CANCEL=3
REREGISTER=4
DEASSIGN=5

#MODES
RUNNING = 1
STOPPING= 2



class TimeoutHTTPConnection(httplib.HTTPConnection):
    def __init__(self,host,timeout=70):
        httplib.HTTPConnection.__init__(self, host, timeout = timeout)

class TimeoutTransport(xmlrpclib.Transport):
    def __init__(self, timeout = 70, *l, **kw):
        xmlrpclib.Transport.__init__(self, *l, **kw)
        self.timeout = timeout

    def make_connection(self, host):
        conn = TimeoutHTTPConnection(host, self.timeout)
        return conn

class TimeoutServerProxy(xmlrpclib.ServerProxy):
    def __init__(self, uri, timeout = 70, *l, **kw):
        kw['transport'] = TimeoutTransport(timeout = timeout, use_datetime = kw.get('use_datetime', 0))
        xmlrpclib.ServerProxy.__init__(self, uri, *l, **kw)

#self register
port = 38864
address = '127.0.0.1'
job_url = 'http://' + address + ':' + str(port + 1)

cache_hostname = None
def get_full_hostname():
    return socket.getfqdn()

def get_hostname():
    #get IP address
    global cache_hostname
    if not cache_hostname is None:
        return cache_hostname
    #adresses = ['google.com', 'nu.nl', 'tweakers.net']
    #while adresses:
    #    try:
    #        socket.setdefaulttimeout(30)
    #        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); 
    #        s.connect((adresses[0], 0)); 
    #        myip = s.getsockname()[0]
    #        s.close()
    #        break
    #    except:
    #        adresses = adresses[1:]

    #print(myip)
    
    #try:
    #    addr=reversename.from_address(myip)
    #    myip = str(resolver.query(addr,"PTR")[0])[:-1]
    #    print(myip)
    #except Exception as e:
    #    myip = socket.gethostname().split('.')[0]
    #    pass
    #
   
    #if '-bb' in myip:
    #    myip = myip.split('-bb')[0]
    myip = get_full_hostname()
    if '.' in myip:
        myip = myip.split('.')[0]
    cache_hostname = myip
   
    return myip

def short_name(name):
    return name.split(".")[0]
    
