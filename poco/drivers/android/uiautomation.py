# coding=utf-8
__author__ = 'lxn3032'


import os
import requests
import time
import warnings
import threading
import atexit

from airtest.core.api import connect_device, device as current_device
from airtest.core.android.ime import YosemiteIme

from hrpc.client import RpcClient
from hrpc.transport.http import HttpTransport
from poco.pocofw import Poco
from poco.agent import PocoAgent
from poco.sdk.Attributor import Attributor
from poco.sdk.interfaces.screen import ScreenInterface
from poco.utils.hrpc.hierarchy import RemotePocoHierarchy
from poco.utils.airtest.input import AirtestInput
from poco.utils import six
from poco.drivers.android.utils.installation import install, uninstall

__all__ = ['AndroidUiautomationPoco', 'AndroidUiautomationHelper']
this_dir = os.path.dirname(os.path.realpath(__file__))
PocoServicePackage = 'com.netease.open.pocoservice'
PocoServicePackageTest = 'com.netease.open.pocoservice.test'


class AndroidRpcClient(RpcClient):
    def __init__(self, endpoint):
        self.endpoint = endpoint
        super(AndroidRpcClient, self).__init__(HttpTransport)

    def initialize_transport(self):
        return HttpTransport(self.endpoint, self)


# deprecated
class AttributorWrapper(Attributor):
    """
    部分手机上仍不支持Accessibility.ACTION_SET_TEXT，使用YosemiteIme还是兼容性最好的方案
    这个class会hook住set_text，然后改用ime的text方法
    """

    def __init__(self, remote, ime):
        self.remote = remote
        self.ime = ime

    def getAttr(self, node, attrName):
        return self.remote.getAttr(node, attrName)

    def setAttr(self, node, attrName, attrVal):
        if attrName == 'text' and attrVal != '':
            # 先清除了再设置，虽然这样不如直接用ime的方法好，但是也能凑合用着
            current_val = self.remote.getAttr(node, 'text')
            if current_val:
                self.remote.setAttr(node, 'text', '')
            self.ime.text(attrVal)
        else:
            self.remote.setAttr(node, attrName, attrVal)


class ScreenWrapper(ScreenInterface):
    def __init__(self, screen):
        super(ScreenWrapper, self).__init__()
        self.screen = screen

    def getScreen(self, width):
        # Android上PocoService的实现为仅返回b64编码的图像，格式固定位jpg
        b64img = self.screen.getScreen(width)
        return b64img, 'jpg'

    def getPortSize(self):
        return self.screen.getPortSize()


class AndroidPocoAgent(PocoAgent):
    def __init__(self, endpoint, ime, use_airtest_input=False):
        self.client = AndroidRpcClient(endpoint)
        remote_poco = self.client.remote('poco-uiautomation-framework')
        dumper = remote_poco.dumper
        selector = remote_poco.selector
        attributor = remote_poco.attributor
        hierarchy = RemotePocoHierarchy(dumper, selector, attributor)

        if use_airtest_input:
            inputer = AirtestInput()
        else:
            inputer = remote_poco.inputer
        super(AndroidPocoAgent, self).__init__(hierarchy, inputer, ScreenWrapper(remote_poco.screen), None)

    def on_bind_driver(self, driver):
        super(AndroidPocoAgent, self).on_bind_driver(driver)
        if isinstance(self.input, AirtestInput):
            self.input.add_preaction_cb(driver)


class AndroidUiautomationPoco(Poco):
    """
    Poco Android implementation for testing **Android native apps**.

    Args:
        device (:py:obj:`Device`): :py:obj:`airtest.core.device.Device` instance provided by ``airtest``. leave the 
         parameter default and the default device will be chosen. more details refer to ``airtest doc``
        using_proxy (:py:obj:`bool`): whether use adb forward to connect the Android device or not
        force_restart (:py:obj:`bool`): whether always restart the poco-service-demo running on Android device or not
        options: see :py:class:`poco.pocofw.Poco`

    Examples:
        The simplest way to initialize AndroidUiautomationPoco instance and no matter your device network status::

            from poco.drivers.android.uiautomation import AndroidUiautomationPoco

            poco = AndroidUiautomationPoco()
            poco('android:id/title').click()
            ...

    """

    def __init__(self, device=None, using_proxy=True, force_restart=False, use_airtest_input=False, **options):
        # 加这个参数为了不在最新的pocounit方案中每步都截图
        self.screenshot_each_action = True
        if options.get('screenshot_each_action') is False:
            self.screenshot_each_action = False

        self.device = device or current_device()
        if not self.device:
            self.device = connect_device("Android:///")

        self.adb_client = self.device.adb
        if using_proxy:
            self.device_ip = self.adb_client.host or "127.0.0.1"
        else:
            self.device_ip = self.device.get_ip_address()

        # save current top activity (@nullable)
        current_top_activity_package = self.device.get_top_activity_name()
        if current_top_activity_package is not None:
            current_top_activity_package = current_top_activity_package.split('/')[0]

        # install ime
        self.ime = YosemiteIme(self.adb_client)
        self.ime.start()

        # install
        self._instrument_proc = None
        self._install_service()

        # forward
        if using_proxy:
            p0, _ = self.adb_client.setup_forward("tcp:10080")
            p1, _ = self.adb_client.setup_forward("tcp:10081")
        else:
            p0 = 10080
            p1 = 10081

        # start
        if self._is_running('com.github.uiautomator'):
            warnings.warn('{} should not run together with "uiautomator". "uiautomator" will be killed.'
                          .format(self.__class__.__name__))
            self.adb_client.shell(['am', 'force-stop', 'com.github.uiautomator'])

        ready = self._start_instrument(p0, force_restart=force_restart)
        if not ready:
            # 启动失败则需要卸载再重启，instrument的奇怪之处
            uninstall(self.adb_client, PocoServicePackage)
            self._install_service()
            ready = self._start_instrument(p0)

            if current_top_activity_package is not None:
                current_top_activity2 = self.device.get_top_activity_name()
                if current_top_activity2 is None or current_top_activity_package not in current_top_activity2:
                    self.device.start_app(current_top_activity_package, activity=True)

            if not ready:
                raise RuntimeError("unable to launch AndroidUiautomationPoco")
        if ready:
            # 首次启动成功后，在后台线程里监控这个进程的状态，保持让它不退出
            self._keep_running_instrumentation(p0)

        endpoint = "http://{}:{}".format(self.device_ip, p1)
        agent = AndroidPocoAgent(endpoint, self.ime, use_airtest_input)
        super(AndroidUiautomationPoco, self).__init__(agent, **options)

    def _install_service(self):
        updated = install(self.adb_client, os.path.join(this_dir, 'lib', 'pocoservice-debug.apk'))
        install(self.adb_client, os.path.join(this_dir, 'lib', 'pocoservice-debug-androidTest.apk'), updated)
        return updated

    def _is_running(self, package_name):
        processes = self.adb_client.shell(['ps']).splitlines()
        for ps in processes:
            ps = ps.strip()
            if ps.endswith(package_name):
                return True
        return False

    def _keep_running_instrumentation(self, port_to_ping):
        print('[pocoservice.apk] background daemon started.')

        def loop():
            while True:
                stdout, stderr = self._instrument_proc.communicate()
                print('[pocoservice.apk] stdout: {}'.format(stdout))
                print('[pocoservice.apk] stderr: {}'.format(stderr))
                print('[pocoservice.apk] retrying instrumentation PocoService')
                self._start_instrument(port_to_ping)  # 尝试重启
                time.sleep(1)
        t = threading.Thread(target=loop)
        t.daemon = True
        t.start()

    def _start_instrument(self, port_to_ping, force_restart=False):
        if not force_restart:
            try:
                state = requests.get('http://{}:{}/uiautomation/connectionState'.format(self.device_ip, port_to_ping),
                                     timeout=10)
                state = state.json()
                if state.get('connected'):
                    # skip starting instrumentation if UiAutomation Service already connected.
                    return True
            except:
                pass

        if self._instrument_proc is not None:
            if self._instrument_proc.poll() is None:
                self._instrument_proc.kill()
            self._instrument_proc = None

        ready = False
        self.adb_client.shell(['am', 'force-stop', PocoServicePackage])

        # 启动instrument之前，先把主类activity启动起来，不然instrumentation可能失败
        self.adb_client.shell('am start -n {}/.TestActivity'.format(PocoServicePackage))

        instrumentation_cmd = [
                'am', 'instrument', '-w', '-e', 'debug', 'false', '-e', 'class',
                '{}.InstrumentedTestAsLauncher'.format(PocoServicePackage),
                '{}.test/android.support.test.runner.AndroidJUnitRunner'.format(PocoServicePackage)]
        self._instrument_proc = self.adb_client.start_shell(instrumentation_cmd)
        atexit.register(self._instrument_proc.kill)
        time.sleep(2)
        for i in range(10):
            try:
                requests.get('http://{}:{}'.format(self.device_ip, port_to_ping), timeout=10)
                ready = True
                break
            except requests.exceptions.Timeout:
                break
            except requests.exceptions.ConnectionError:
                if self._instrument_proc.poll() is not None:
                    warnings.warn("[pocoservice.apk] instrumentation test server process is no longer alive")
                    stdout = self._instrument_proc.stdout.read()
                    stderr = self._instrument_proc.stderr.read()
                    print('[pocoservice.apk] stdout: {}'.format(stdout))
                    print('[pocoservice.apk] stderr: {}'.format(stderr))
                time.sleep(1)
                print("still waiting for uiautomation ready.")
                continue
        return ready

    def on_pre_action(self, action, ui, args):
        if self.screenshot_each_action:
            # airteset log用
            from airtest.core.api import snapshot
            msg = repr(ui)
            if not isinstance(msg, six.text_type):
                msg = msg.decode('utf-8')
            snapshot(msg=msg)


class AndroidUiautomationHelper(object):
    _nuis = {}

    @classmethod
    def get_instance(cls, device):
        """
        This is only a slot to store and get already initialized poco instance rather than initializing again. You can
        simply pass the ``current device instance`` provided by ``airtest`` to get the AndroidUiautomationPoco instance.
        If no such AndroidUiautomationPoco instance, a new instance will be created and stored. 

        Args:
            device (:py:obj:`airtest.core.device.Device`): more details refer to ``airtest doc``

        Returns:
            poco instance
        """

        if cls._nuis.get(device) is None:
            cls._nuis[device] = AndroidUiautomationPoco(device)
        return cls._nuis[device]
