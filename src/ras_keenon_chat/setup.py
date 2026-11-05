from glob import glob
from setuptools import setup
name='ras_keenon_chat'
setup(name=name,version='0.1.0',packages=[name],install_requires=['setuptools'],
      package_data={name:['static/*.html','static/*.css','static/*.js']},
      data_files=[('share/ament_index/resource_index/packages',['resource/'+name]),
                  ('share/'+name,['package.xml','README.md']),
                  ('share/'+name+'/launch',glob('launch/*.launch.py'))],
      maintainer='RAS operator',maintainer_email='operator@example.com',license='Apache-2.0',
      entry_points={'console_scripts':['chat_server = '+name+'.server:main']})
