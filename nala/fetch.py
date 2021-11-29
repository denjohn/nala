from urllib.request import urlopen
import struct
import select
import socket
import time
import random
import re
import threading
from click import style
from nala.utils import NALA_SOURCES, ask, shell
from nala.options import arg_parse
netselect_scored = []

def chk(data):
	x = sum(x << 8 if i % 2 else x for i, x in enumerate(data)) & 0xFFFFFFFF
	x = (x >> 16) + (x & 0xFFFF)
	x = (x >> 16) + (x & 0xFFFF)
	return struct.pack('<H', ~x & 0xFFFF)

def ping(addr, timeout=2, number=1, data=b''):
	with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as conn:
		payload = struct.pack('!HH', random.randrange(0, 65536), number) + data

		conn.connect((addr, 80))
		conn.sendall(b'\x08\0' + chk(b'\x08\0\0\0' + payload) + payload)
		start = time.time()

		while select.select([conn], [], [], max(0, start + timeout - time.time()))[0]:
			data = conn.recv(65536)
			if len(data) < 20 or len(data) < struct.unpack_from('!xxH', data)[0]:
				continue
			if data[20:] == b'\0\0' + chk(b'\0\0\0\0' + payload) + payload:
				return time.time() - start

def net_select(host):
	try:
		# Regex to get the domain
		regex = re.search('https?://([A-Za-z_0-9.-]+).*', host)
		if regex:
			domain = regex.group(1)
		#res = ping(host.replace('http://', '').replace('/debian/', '').strip('/'))
		res = ping(domain)
		if res:
			res = str(int(res*1000))
			if len(res) == 2:
				res = '0'+res
			if len(res) == 1:
				res == '00'+res
			netselect_scored.append(f'{res} {host}')
	except (socket.gaierror, OSError, ConnectionRefusedError) as e:
		e = str(e)
		regex = re.search('\[.*\]', e)
		if regex: 
			e = style(e.replace(regex.group(0), '').strip(), fg='yellow', bold=True)
		print(f'{e}: {domain}')
		print(f"{style('URL:', fg='yellow', bold=True)} {host}\n")

def parse_ubuntu(country: list=None):
	print('Fetching Ubuntu mirrors...')
	ubuntu = urlopen('https://launchpad.net/ubuntu/+archivemirrors')
	ubuntu = "".join(line.decode() for line in ubuntu).split('\n\n')
	mirror_list = []

	if country is None:
		country = []
		for section in ubuntu:
			for line in section.strip().splitlines():
				if '<th colspan="2">' in line:
					line = line[line.index('>')+1:]
					line = line[:line.index('<')]
					country.append(line)

	print('Parsing mirror list...')
	for country in country:
		for section in ubuntu:
			if country in section:
				for mirror in section.split('<tr>'):
					for line in mirror.splitlines():
						if '"http://' in line and '">http</a>' in line:
							# It matched so we now we strip off everything before and after the quotes
							line = line[line.index('"')+1:]
							line = line[:line.index('"')]
							mirror_list.append(line)
	# Convert to set to remove any duplicates
	mirror_list = set(mirror_list)
	return list(mirror_list)

def parse_debian(country: list=None):
	arch = shell.dpkg.__print_architecture().stdout.strip()
	print('Fetching Debian mirrors...')
	debian = urlopen('https://www.debian.org/mirror/list-full')
	debian = "".join(line.decode() for line in debian).split('<h3><a ')
	mirror_list = []

	if country is None:
		country = []
		for section in debian:
			for line in section.splitlines():
				if line.startswith('name="'):
					line = line[line.index('>')+1:]
					line = line[:line.index('<')]
					country.append(line)

	print('Parsing mirror list...')
	for country in country:
		for section in debian:
			if country in section:
				for mirror in section.split('<br><br>'):
					if 'Packages over HTTP:' in mirror and arch in mirror:
						for line in mirror.splitlines():
							if line.startswith('Packages over HTTP:'):
								for line in line.split():
									match = re.match(".*(http\S+)", line)
									if match:
										# It matched so we now we strip off everything before and after the quotes
										line = line[line.index('"')+1:]
										line = line[:line.index('"')]
										mirror_list.append(line)
	# Convert to set to remove any duplicates
	mirror_list = set(mirror_list)
	return list(mirror_list)

def detect_release():
	distro = False
	release = False
	release = shell.apt.policy(capture_output=True, text=True).stdout.splitlines()
	for line in release:
		if 'o=Ubuntu' in line or 'o=Debian' in line:
			release = line.split(',')
			for line in release:
				if line == 'o=Ubuntu':
					distro = 'ubuntu'
					for line in release:
						if line.startswith('n='):
							release = line[2:]
	if distro and release:
		return distro, release
	else:
		parser = arg_parse()
		err = style('Error:', fg='red', bold=True)
		print(f'{err} Unable to detect release. Specify manually')
		parser.parse_args(['fetch', '--help'])
		exit(1)

def fetch(	fetches: int, foss: bool = False,
			debian=None, ubuntu=None, country=None,
			assume_yes=False):
	"""Fetches fast mirrors and write to nala-sources.list"""
	if NALA_SOURCES.exists():
		if not assume_yes:
			if not ask(f'{NALA_SOURCES.name} already exists.\ncontinue and overwrite it'):
				print('Abort')
				exit()

	# Make sure there aren't any shenanigans
	if fetches not in range(1,11):
		print('Amount of fetches has to be 1-10...')
		exit(1)

	# If supplied a country it needs to be a list
	if country:
		country = [country]

	if not debian and not ubuntu:
		distro, release = detect_release()
	elif debian:
		distro = 'debian'
		release = debian
	elif ubuntu:
		distro = 'ubuntu'
		release = ubuntu
	else:
		print('Something went wrong...')
		exit(1)

	if distro == 'debian':
		netselect = parse_debian(country)
		component = 'main contrib non-free'
		if foss:
			component = 'main'
	else:
		netselect = parse_ubuntu(country)
		# It's ubuntu, you probably don't care about foss
		component = 'main restricted universe multiverse'

	thread_handler = []
	num = -1
	for url in netselect:
		num = num +1
		thread = threading.Thread(name='Net Select', target=net_select, args=[netselect[num]])
		thread_handler.append(thread)
		thread.start()

	print('Testing Urls...')
	# wait for all our threads to stop
	for thread in thread_handler:
		thread.join()

	netselect_scored.sort()

	num = 0
	with open(NALA_SOURCES, 'w') as file:
		print(f'Writing {NALA_SOURCES}\n')
		print(f'# Sources file built for nala\n', file=file)
		for line in netselect_scored:
			num = num + 1
			line = line[line.index('h'):]
			print(f'deb {line} {release} {component}')
			print(f'deb-src {line} {release} {component}\n')
			print(f'deb {line} {release} {component}', file=file)
			print(f'deb-src {line} {release} {component}\n', file=file)
			if num == fetches:
				break
