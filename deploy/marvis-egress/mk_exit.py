"""建一个新的 WARP 出口：wgcf 注册 → 生成 wireproxy.conf。

用法：python3 mk_exit.py <socks_port> <http_port> <name>
    例：python3 mk_exit.py 41001 42001 w1

只有首次建出口用；日常换 IP 走 refresh_exits.sh。
"""
import os
import re
import subprocess
import sys

BIN = '/opt/marvis-egress/bin'
EXITS = '/opt/marvis-egress/exits'

if len(sys.argv) != 4:
    print(__doc__)
    sys.exit(2)
SOCKS_PORT, HTTP_PORT, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]

d = os.path.join(EXITS, name)
os.makedirs(d, exist_ok=True)
os.chdir(d)


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


if not os.path.exists('wgcf-account.toml'):
    r = run(BIN + '/wgcf register --accept-tos')
    print('register rc=', r.returncode, (r.stdout + r.stderr).strip()[:200])
else:
    print('account exists')

r = run(BIN + '/wgcf generate')
print('generate rc=', r.returncode, (r.stdout + r.stderr).strip()[:200])
conf = open('wgcf-profile.conf').read()


def grab(section, key):
    m = re.search(r'^\[' + section + r'\]$(.*?)(?=^\[|\Z)', conf, re.S | re.M)
    if not m:
        return None
    mm = re.search(r'^' + key + r'\s*=\s*(.+)$', m.group(1), re.M)
    return mm.group(1).strip() if mm else None


addr = grab('Interface', 'Address')
priv = grab('Interface', 'PrivateKey')
dns = grab('Interface', 'DNS') or '1.1.1.1'
pub = grab('Peer', 'PublicKey')
ep = grab('Peer', 'Endpoint')
print('addr=', addr)
print('endpoint=', ep)
if not (addr and priv and pub and ep):
    print('!! profile 解析失败')
    sys.exit(2)

wp = ('[Interface]\nAddress = %s\nPrivateKey = %s\nDNS = %s\n\n'
      '[Peer]\nPublicKey = %s\nEndpoint = %s\nAllowedIPs = 0.0.0.0/0, ::/0\n\n'
      '[Socks5]\nBindAddress = 127.0.0.1:%d\n') % (addr, priv, dns, pub, ep, SOCKS_PORT)
open('wireproxy.conf', 'w').write(wp)
print('wrote wireproxy.conf')
