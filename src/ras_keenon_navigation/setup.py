from glob import glob
from setuptools import setup
name='ras_keenon_navigation'
setup(name=name,version='0.1.0',packages=[name],install_requires=['setuptools','numpy','scipy'],
      data_files=[('share/ament_index/resource_index/packages',['resource/'+name]),
                  ('share/'+name,['package.xml','README.md']),
                  ('share/'+name+'/launch',glob('launch/*.launch.py')),
                  ('share/'+name+'/config',glob('config/*.json'))],
      maintainer='RAS operator',maintainer_email='operator@example.com',license='Apache-2.0',
      entry_points={'console_scripts':['navigation_server = '+name+'.server:main']})
