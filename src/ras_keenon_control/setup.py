from glob import glob
from setuptools import setup

name = 'ras_keenon_control'
setup(name=name, version='0.1.0', packages=[name],
      data_files=[('share/ament_index/resource_index/packages', ['resource/'+name]),
                  ('share/'+name, ['package.xml']),
                  ('share/'+name+'/launch', glob('launch/*.launch.py')),
                  ('share/'+name+'/config', glob('config/*.yaml'))],
      install_requires=['setuptools'], zip_safe=True,
      maintainer='RAS operator', maintainer_email='operator@example.com',
      description='XMAN manual and vision control interfaces', license='Apache-2.0',
      entry_points={'console_scripts': [
          'controller = ras_keenon_control.driver:main',
          'keyboard = ras_keenon_control.keyboard:main',
          'vision_example = ras_keenon_control.vision_example:main']})
