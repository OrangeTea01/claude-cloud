import io
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer import core
from scripts import install_solo as remote


def form(**extra):
    return {'host': '93.184.216.34', 'port': '22', 'username': 'root', 'auth': 'password',
            'ssh_password': 'ssh-fixture-secret', 'site_password': 'Website-fixture-123',
            'confirm_password': 'Website-fixture-123', 'profile': 'auto', **extra}


def config(**extra):
    return {'attempt_id': 'a' * 32, 'address': '93.184.216.34', 'profile': 'auto',
            'site_password': 'Website-fixture-123', 'payload_sha256': 'b' * 64, **extra}


class Key:
    def __init__(self, value=b'key'): self.value=value
    def asbytes(self): return self.value
    def get_name(self): return 'ssh-ed25519'
    def get_base64(self): return self.value.hex()


class ValidationTests(unittest.TestCase):
    def test_normal_password_and_default_public_address(self):
        settings=core.Settings.from_form(form())
        self.assertEqual(settings.address, '93.184.216.34')
        self.assertEqual(settings.port,22)
        self.assertNotIn('fixture-secret',repr(settings))
        settings.erase_secrets()
        self.assertFalse(settings.site_password or settings.ssh_password)

    def test_rejects_code_ports_private_targets_and_bad_passwords(self):
        for change in ({'host':'x;id'}, {'host':'https://93.184.216.34'}, {'host':'127.0.0.1'},
                       {'host':'a\nexample.com'}, {'username':'root;id'}, {'port':'65536'}, {'port':'x'},
                       {'site_password':'short'}, {'confirm_password':'wrong'}, {'sudo_password':'secret\nline'}):
            with self.subTest(change=change), self.assertRaises(core.DeploymentError): core.Settings.from_form(form(**change))

    def test_key_auth_checks_file_and_allows_empty_ssh_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            file=Path(temporary)/'id_ed25519';file.write_text('fixture')
            settings=core.Settings.from_form(form(auth='key',key_path=str(file),ssh_password=''))
            self.assertEqual(settings.key_path,str(file))
        with self.assertRaises(core.DeploymentError):core.Settings.from_form(form(auth='key',key_path='does-not-exist'))

    def test_redaction_removes_secrets_hash_and_ansi(self):
        settings=core.Settings.from_form(form())
        message='\x1b[31m'+settings.site_password+' '+settings.ssh_password+' '+remote.password_hash(settings.site_password)
        result=core.redact(message,settings)
        self.assertNotIn(settings.site_password,result);self.assertNotIn(settings.ssh_password,result)
        self.assertNotIn('scrypt$',result);self.assertNotIn('\x1b',result)

    def test_public_targets_reject_malformed_ipv4_and_accept_idna(self):
        for value in ('1.2.3.999','a.example.com:443','a..example.com','localhost','224.0.0.1'):
            with self.assertRaises(core.DeploymentError):core.canonical_host(value)
            with self.assertRaises(remote.InstallError):remote.public_address(value)
        self.assertEqual(core.canonical_host('CLOUD.Example.COM'),'cloud.example.com')


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.json'
    def tearDown(self):self.temp.cleanup()

    def test_first_key_requires_confirmation_and_same_key_does_not(self):
        state=core.LocalState(self.path);calls=[]
        state.confirm_key('host',Key(),lambda host,fp:calls.append(fp) or True)
        core.LocalState(self.path).confirm_key('host',Key(),lambda *_:self.fail('Repeated prompt'))
        self.assertEqual(len(calls),1);self.assertTrue(calls[0].startswith('SHA256:'))
        self.assertNotIn('password',self.path.read_text())

    def test_changed_key_fails_even_if_callback_would_accept(self):
        state=core.LocalState(self.path);state.confirm_key('host',Key(),lambda *_:True)
        with self.assertRaises(core.DeploymentError):state.confirm_key('host',Key(b'changed'),lambda *_:True)

    def test_declined_key_not_saved_and_attempt_survives_restart(self):
        state=core.LocalState(self.path)
        with self.assertRaises(core.Cancelled):state.confirm_key('host',Key(),lambda *_:False)
        self.assertFalse(self.path.exists())
        settings=core.Settings.from_form(form());attempt=state.attempt(settings)
        self.assertEqual(core.LocalState(self.path).attempt(settings),attempt)
        self.assertNotIn(settings.site_password,self.path.read_text())
        self.assertNotIn(settings.ssh_password,self.path.read_text())


class Channel:
    def __init__(self, *, sudo=False, bootstrap=False, url='https://93.184.216.34', code=0):
        self.stdout=[];self.stderr=[b'CC_SUDO_PROMPT\\n'] if sudo else []
        self.sudo=sudo;self.bootstrap=bootstrap;self.url=url;self.code=code
        self.sent=[];self.command=None;self.closed=False;self.finished=False
        if not sudo:self.start()
    def start(self):
        if self.bootstrap:
            self.stdout.append(b'CC_EVENT {"event":"bootstrap_complete"}\n');self.finished=True
        else:self.stdout.append(b'CC_EVENT {"event":"ready"}\n')
    def settimeout(self,n):pass
    def exec_command(self,command):self.command=command
    def sendall(self,data):
        self.sent.append(data)
        if self.sudo and len(self.sent)==1:
            self.start();return
        value=json.loads(data)
        self.stdout.append(('CC_EVENT '+json.dumps({'event':'log','message':value['site_password']})+'\n').encode())
        self.stdout.append(('CC_EVENT '+json.dumps({'event':'complete','url':self.url,'path':'/opt/claude-cloud-solo','profile':'8g','installationId':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'})+'\n').encode())
        self.finished=True
    def shutdown_write(self):pass
    def recv_ready(self):return bool(self.stdout)
    def recv_stderr_ready(self):return bool(self.stderr)
    def recv(self,n):return self.stdout.pop(0)
    def recv_stderr(self,n):return self.stderr.pop(0)
    def exit_status_ready(self):return self.finished
    def recv_exit_status(self):return self.code
    def close(self):self.closed=True


class StreamTests(unittest.TestCase):
    def run_stream(self,channel,**kwargs):
        settings=core.Settings.from_form(form(username='debian'))
        events=[]
        deployer=core.Deployer('.',None,lambda kind,**data:events.append((kind,data)),lambda *_:True)
        deployer.client=SimpleNamespace(get_transport=lambda:SimpleNamespace(open_session=lambda **_:channel))
        result=deployer.stream('fixed-safe-command',None if kwargs.get('bootstrap') else config(),settings,**kwargs)
        return result,settings,events

    def test_sudo_password_only_after_prompt_config_only_after_ready(self):
        channel=Channel(sudo=True)
        result,settings,events=self.run_stream(channel,sudo=True)
        self.assertEqual(channel.sent[0],(settings.ssh_password+'\n').encode())
        self.assertEqual(json.loads(channel.sent[1])['site_password'],settings.site_password)
        self.assertNotIn(settings.site_password,channel.command)
        self.assertNotIn(settings.site_password,str(events))
        self.assertTrue(channel.closed);self.assertEqual(result['url'],'https://93.184.216.34')

    def test_passwordless_sudo_does_not_receive_password_before_json(self):
        channel=Channel()
        self.run_stream(channel,sudo=True)
        self.assertEqual(len(channel.sent),1)
        self.assertIsInstance(json.loads(channel.sent[0]),dict)

    def test_bootstrap_supports_root_and_password_sudo_without_site_config(self):
        for sudo in (False,True):
            channel=Channel(sudo=sudo,bootstrap=True)
            result,settings,_=self.run_stream(channel,sudo=sudo,bootstrap=True)
            self.assertTrue(result['bootstrap'])
            self.assertEqual(len(channel.sent),int(sudo))
            self.assertNotIn(settings.site_password,str(channel.sent))

    def test_wrong_result_url_is_rejected(self):
        with self.assertRaises(core.DeploymentError):self.run_stream(Channel(url='https://other.example.com'))

    def test_failed_exit_does_not_report_success(self):
        with self.assertRaises(core.DeploymentError):self.run_stream(Channel(code=1))

    def test_other_installation_health_cannot_complete(self):
        channel=Channel()
        original=channel.sendall
        def replace(data):
            original(data)
            channel.stdout[-1]=channel.stdout[-1].replace(b'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',b'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
        channel.sendall=replace
        with self.assertRaises(core.DeploymentError):self.run_stream(channel)

    def test_root_wrapper_verifies_exact_bytes_and_uses_isolated_python(self):
        source=core.ROOT_STAGE_WRAPPER
        self.assertIn("hashlib.sha256(data).hexdigest() != expected",source)
        self.assertIn("[sys.executable, '-I'",source)
        self.assertIn('os.O_NOFOLLOW',source)
        self.assertIn("dir='/var/tmp'",source)


class PayloadTests(unittest.TestCase):
    def test_manifest_detects_missing_or_tampered_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);payload=base/'payload';payload.mkdir()
            manifest={}
            for name in ('claude-cloud-solo.zip','install_solo.py'):
                raw=name.encode();(payload/name).write_bytes(raw);manifest[name]=hashlib.sha256(raw).hexdigest()
            (payload/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(len(core.payload_files(base)[0]),2)
            (payload/'install_solo.py').write_bytes(b'changed')
            with self.assertRaises(core.DeploymentError):core.payload_files(base)

    def test_privileged_wrapper_copies_verified_bytes_before_execution_and_cleans(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);source=base/'source';source.mkdir();destination=base/'root-owned'
            source.joinpath('install_solo.py').write_bytes(b'trusted helper')
            source.joinpath('claude-cloud-solo.zip').write_bytes(b'trusted archive')
            hashes=[hashlib.sha256((source/name).read_bytes()).hexdigest() for name in ('install_solo.py','claude-cloud-solo.zip')]
            def make_temp(**kwargs):destination.mkdir(mode=0o700);return str(destination)
            def child(args):
                self.assertEqual(args[1],'-I');self.assertEqual(Path(args[2]).parent,destination)
                self.assertEqual(Path(args[2]).read_bytes(),b'trusted helper')
                self.assertEqual(Path(args[4]).read_bytes(),b'trusted archive')
                return SimpleNamespace(returncode=0)
            with patch.object(core.os,'geteuid',return_value=0,create=True),patch.object(core.os,'O_NOFOLLOW',getattr(core.os,'O_NOFOLLOW',0),create=True),patch.object(tempfile,'mkdtemp',side_effect=make_temp),patch.object(subprocess,'run',side_effect=child),patch.object(sys,'argv',['wrapper',str(source),*hashes]),self.assertRaises(SystemExit) as error:
                exec(core.ROOT_STAGE_WRAPPER,{})
            self.assertEqual(error.exception.code,0);self.assertFalse(destination.exists())
            # Tampering the login-user-owned upload cannot reach the privileged child.
            source.joinpath('install_solo.py').write_bytes(b'tampered helper')
            with patch.object(core.os,'geteuid',return_value=0,create=True),patch.object(core.os,'O_NOFOLLOW',getattr(core.os,'O_NOFOLLOW',0),create=True),patch.object(tempfile,'mkdtemp',side_effect=make_temp),patch.object(subprocess,'run') as execute,patch.object(sys,'argv',['wrapper',str(source),*hashes]),self.assertRaises(ValueError):
                exec(core.ROOT_STAGE_WRAPPER,{})
            execute.assert_not_called();self.assertFalse(destination.exists())


class DeploymentFlowTests(unittest.TestCase):
    def test_missing_python_bootstraps_then_uploads_and_runs_verified_wrapper(self):
        for username in ('root','debian'):
            with self.subTest(username=username),tempfile.TemporaryDirectory() as temporary:
                base=Path(temporary);payload=base/'payload';payload.mkdir();manifest={}
                for name in ('claude-cloud-solo.zip','install_solo.py'):
                    raw=name.encode();payload.joinpath(name).write_bytes(raw);manifest[name]=hashlib.sha256(raw).hexdigest()
                payload.joinpath('manifest.json').write_text(json.dumps(manifest))
                events=[];opened=[];commands=[]
                class Upload(io.BytesIO):
                    def __exit__(self,*args):opened.append(self.getvalue());self.close()
                sftp=SimpleNamespace(mkdir=lambda *a,**k:None,chmod=lambda *a:None,open=lambda *a:Upload(),remove=lambda *a:None,rmdir=lambda *a:None,close=lambda:None)
                class Runner(core.Deployer):
                    def connect(self,settings):self.client=SimpleNamespace(open_sftp=lambda:sftp,close=lambda:None,get_transport=lambda:SimpleNamespace(get_remote_server_key=lambda:Key()))
                    def basic(self,command,**kwargs):
                        commands.append(command)
                        if command.endswith('id -u'):return (0,'0' if username=='root' else '1000','')
                        if command.endswith('/usr/bin/python3'):return (1,'','')
                        return (0,'','')
                    def stream(self,command,cfg,settings,**kwargs):
                        commands.append(command)
                        if kwargs.get('bootstrap'):
                            self_test.assertIn('apt-get install -y python3',command)
                            self_test.assertIsNone(cfg)
                            return {'bootstrap':True}
                        self_test.assertIn('/usr/bin/python3 -I -c',command)
                        self_test.assertIn(manifest['install_solo.py'],command)
                        self_test.assertNotIn(settings.site_password,command)
                        self_test.assertNotIn(settings.ssh_password,command)
                        self_test.assertEqual(cfg['attempt_id'],state.attempt(settings))
                        return {'url':'https://'+settings.address,'path':'/opt/claude-cloud-solo','installationId':'a-id'}
                self_test=self
                state=core.LocalState(base/'state.json')
                runner=Runner(base,state,lambda kind,**data:events.append((kind,data)),lambda *_:True,health=lambda *_:False)
                result=runner.run(core.Settings.from_form(form(username=username)))
                self.assertEqual(len(opened),2)
                self.assertTrue(any('apt-get install -y python3' in c for c in commands))
                complete=[data for kind,data in events if kind=='complete'][0]
                self.assertFalse(complete['local_ok']);self.assertEqual(result['url'],'https://93.184.216.34')


class HealthTests(unittest.TestCase):
    def response(self,body):
        class Reply:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(body).encode()
        return Reply()

    def test_local_https_requires_exact_nonsecret_installation_id(self):
        for body,expected in (({'ok':True},False),({'ok':True,'installationId':'other'},False),({'ok':True,'installationId':'expected'},True)):
            opener=SimpleNamespace(open=lambda *a,**k:self.response(body))
            with patch.object(core.urllib.request,'build_opener',return_value=opener):
                self.assertEqual(core.local_health('https://93.184.216.34','expected'),expected)

    def test_remote_public_https_requires_same_instance(self):
        for body,expected in (({'ok':True,'installationId':'other'},False),({'ok':True,'installationId':'expected'},True)):
            opener=SimpleNamespace(open=lambda *a,**k:self.response(body))
            with patch.object(remote.urllib.request,'build_opener',return_value=opener):
                self.assertEqual(remote.public_instance_health('https://93.184.216.34','expected'),expected)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_config_requires_strict_fields_and_never_accepts_injection(self):
        self.assertEqual(remote.parse_config(json.dumps(config()))['profile'],'auto')
        for change in ({'target':'/etc'},{'address':'a.com;id'},{'attempt_id':'../../x'},{'site_password':'short'}, {'profile':[]}):
            with self.assertRaises(remote.InstallError):remote.parse_config(json.dumps(config(**change)))

    def test_resource_profiles_are_bounded(self):
        self.assertEqual(remote.select_profile('auto',8*1024*1024),'8g')
        self.assertEqual(remote.select_profile('auto',24*1024*1024),'24g')
        with self.assertRaises(remote.InstallError):remote.select_profile('24g',8*1024*1024)
        with self.assertRaises(remote.InstallError):remote.select_profile('auto',2*1024*1024)

    def test_render_solo_env_uses_hash_not_plaintext_and_owned_override(self):
        cfg=config();encoded=remote.password_hash(cfg['site_password'])
        body=remote.render_env(cfg,'8g',encoded)
        self.assertIn('APP_MODE=solo\n',body)
        self.assertIn('MANAGEMENT_PASSWORD_HASH=\n',body)
        self.assertIn('COMPOSE_FILE=compose.yaml:.oneclick.compose.yaml',body)
        self.assertNotIn(cfg['site_password'],body)
        override=json.loads(remote.compose_override(cfg['attempt_id']))
        for kind in ('services','volumes','networks'):
            self.assertTrue(all(v['labels'][remote.INSTALL_LABEL]==cfg['attempt_id'] for v in override[kind].values()))

    def make_existing(self):
        cfg=config();self.root.joinpath('.env').write_text(remote.render_env(cfg,'8g',remote.password_hash(cfg['site_password'])))
        marker={'kind':'claude-cloud-solo-v1','attempt_id':cfg['attempt_id'],'payload_sha256':cfg['payload_sha256'],'address':cfg['address'],'profile':'8g','state':'configured','docker_preexisting':False}
        self.root.joinpath(remote.MARKER).write_text(json.dumps(marker))
        return cfg

    def test_resume_requires_own_attempt_same_payload_address_and_original_password(self):
        cfg=self.make_existing()
        self.assertEqual(remote.resume_config(self.root,cfg)['profile'],'8g')
        for change in ({'attempt_id':'c'*32},{'payload_sha256':'c'*64},{'address':'other.example.com'},{'site_password':'Other-password-456'}):
            with self.assertRaises(remote.InstallError):remote.resume_config(self.root,{**cfg,**change})

    def test_unknown_existing_installation_never_adopted(self):
        self.root.joinpath('.env').write_text('PRIVATE ORIGINAL FILE')
        with self.assertRaises(remote.InstallError):remote.resume_config(self.root,config())
        self.assertEqual(self.root.joinpath('.env').read_text(),'PRIVATE ORIGINAL FILE')
        self.root.joinpath(remote.MARKER).write_text('[]')
        with self.assertRaises(remote.InstallError):remote.resume_config(self.root,config())
        cfg=self.make_existing()
        marker=json.loads(self.root.joinpath(remote.MARKER).read_text())
        marker['profile']=[]
        self.root.joinpath(remote.MARKER).write_text(json.dumps(marker))
        with self.assertRaises(remote.InstallError):remote.resume_config(self.root,cfg)

    def archive(self,extra=None):
        path=self.root/'payload.zip'
        with zipfile.ZipFile(path,'w') as output:
            for name in ('compose.yaml','scripts/deploy_public.py','backend/app.py'):
                output.writestr('claude-cloud/'+name,'fixture-source')
            if extra:output.writestr(*extra)
        return path,hashlib.sha256(path.read_bytes()).hexdigest()

    def test_safe_archive_and_exact_verified_bytes_used_after_path_changes(self):
        path,digest=self.archive();raw=path.read_bytes();destination=self.root/'extract';destination.mkdir()
        original=remote.read_regular
        def changing(file,*args):
            result=original(file,*args)
            path.write_bytes(b'replaced after validation')
            return result
        with patch.object(remote,'read_regular',side_effect=changing):
            project=remote.stage_source(path,digest,destination)
        self.assertEqual((project/'backend/app.py').read_text(),'fixture-source')

    def test_archive_rejects_traversal_symlinks_special_files_and_private_config(self):
        for name in ('claude-cloud/../outside','claude-cloud/a/./b','claude-cloud//oops','/absolute','claude-cloud/.env'):
            path,digest=self.archive((name,'bad'))
            with self.assertRaises(remote.InstallError):remote.stage_source(path,digest,self.root/'out')
        for mode in (stat.S_IFLNK,stat.S_IFIFO,stat.S_IFSOCK):
            item=zipfile.ZipInfo('claude-cloud/link');item.external_attr=(mode|0o777)<<16
            path,digest=self.archive((item,'target'))
            with self.assertRaises(remote.InstallError):remote.stage_source(path,digest,self.root/'out')

    def test_foreign_app_volume_is_not_adopted_on_retry(self):
        def run(args,**kwargs):
            if args[1:3]==['volume','inspect'] and args[-1]=='claude-cloud_app_data':
                return SimpleNamespace(returncode=0,stdout=json.dumps([{'Labels':{'com.docker.compose.project':'claude-cloud'}}]),stderr='')
            if 'inspect' in args:return SimpleNamespace(returncode=1,stdout='',stderr='no such object')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(remote.subprocess,'run',side_effect=run),self.assertRaises(remote.InstallError):
            remote.check_owned_resources(Path('/opt/claude-cloud-solo'),'a'*32)

    def test_empty_docker_resources_pass_and_failed_inspection_fails_closed(self):
        def empty(args,**kwargs):
            return SimpleNamespace(returncode=int('inspect' in args),stdout='',stderr='no such object')
        with patch.object(remote.subprocess,'run',side_effect=empty):remote.check_owned_resources(Path('/opt/claude-cloud-solo'),'a'*32)
        with patch.object(remote.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='permission denied')),self.assertRaises(remote.InstallError):
            remote.check_owned_resources(Path('/opt/claude-cloud-solo'),'a'*32)

    @unittest.skipUnless(sys.platform=='linux','Linux process groups required')
    def test_timeout_kills_grandchild_holding_stdout(self):
        import time
        started=time.monotonic()
        script="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"
        with self.assertRaises(remote.InstallError):remote.run([sys.executable,'-c',script],timeout=.2)
        self.assertLess(time.monotonic()-started,5)


if __name__=='__main__':unittest.main()
