from glob import glob
from setuptools import setup
name = 'ras_keenon_commands'
setup(name=name, version='0.1.0', packages=[name],
      data_files=[('share/ament_index/resource_index/packages', ['resource/'+name]),
                  ('share/'+name, ['package.xml']),
                  ('share/'+name+'/launch', glob('launch/*.launch.py')),
                  ('share/'+name+'/config', glob('config/*.yaml'))],
      package_data={name: ['panel.html','battery.js','mapping.js','mapping.css']}, install_requires=['setuptools'], zip_safe=True,
      maintainer='RAS operator', maintainer_email='operator@example.com',
      description='Parameterized XMAN component commands', license='Apache-2.0',
      entry_points={'console_scripts': ['server = ras_keenon_commands.server:main',
                                       'arm_status = ras_keenon_commands.arm_status:main',
                                       'terminal = ras_keenon_commands.terminal:main']})
