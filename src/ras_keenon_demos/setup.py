from glob import glob
from setuptools import setup
name='ras_keenon_demos'
actions=['shake_hand','wave_large','raise_arm','turn_head','nod','shake_head','squat','stand','reset','move_forward','rotate','stop']
setup(name=name,version='0.1.0',packages=[name],install_requires=['setuptools'],
      data_files=[('share/ament_index/resource_index/packages',['resource/'+name]),
                  ('share/'+name,['package.xml','README.md']),
                  ('share/'+name+'/launch',glob('launch/*.launch.py')),
                  ('share/'+name+'/scripts',glob('scripts/*.py')),
                  ('share/'+name+'/examples',glob('examples/*.py'))],
      maintainer='RAS operator',maintainer_email='operator@example.com',license='Apache-2.0',
      entry_points={'console_scripts':['action_server = '+name+'.api:main']+
                    [a+' = '+name+'.entrypoints:'+a for a in actions]})
