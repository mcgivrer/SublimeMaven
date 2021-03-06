# All of SublimeMaven is licensed under the MIT license.

#   Copyright (c) 2012 Nick Lloyd

#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:

#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.

#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#   THE SOFTWARE.

import sublime
import os
import json
import threading
import xml.sax
import string
import subprocess
import re
from StringIO import StringIO

non_cp_mvn_output_pattern = re.compile('^\[[A-Z]+\] ')

'''
Recursive call to find (and return) the nearest path in the current
tree (searching up the path tree) to a pom.xml file.
Returns None if we hit the root without hitting a pom.xml file.
'''
def find_nearest_pom(path):
    cur_path = None
    if os.path.isdir(path):
        cur_path = path
    else:
        cur_path = os.path.dirname(path)

    if os.path.isfile(os.path.join(cur_path, 'pom.xml')):
        return cur_path
    else:
        parent,child = os.path.split(cur_path)
        if len(child) == 0:
            return None
        else:
            return find_nearest_pom(parent)

class PomHandler(xml.sax.ContentHandler):
    elements = []
    groupId = None
    artifactId = None

    def get_project_name(self, long_name = False):
        if not long_name:
            groupid_bits = self.groupId.split('.')
            new_groupid = []
            for bit in groupid_bits:
                new_groupid.append(bit[0])
            self.groupId = string.join(new_groupid, '.')
        return '%s:%s:PROJECT' % (self.groupId, self.artifactId)

    def startElement(self, name, attrs):
        self.elements.append(name)

    def characters(self, content):
        # grab parent groupId first as child groupId defaults to parent if not present
        if len(self.elements) == 3:
            if self.elements[-1] == 'groupId':
                self.groupId = content
        elif len(self.elements) == 2:
            if self.elements[-1] == 'groupId':
                self.groupId = content
            elif self.elements[-1] == 'artifactId':
                self.artifactId = content

    def endElement(self, name):
        self.elements.pop()


'''
Use 'mvn -N dependency:build-classpath' to generate the classpath for the specified pom file
'''
class MvnClasspathGrabbingThread(threading.Thread):
    def __init__(self, pom_path):
        self.pom_path = pom_path
        self.classpath = set()
        self.dest_classpath = None
        threading.Thread.__init__(self)

    def run(self):
        curdir = os.getcwd()
        os.chdir(self.pom_path)
        mvn = None
        if os.name == 'nt':
            mvn = 'mvn.bat'
        else:
            mvn = 'mvn'
        # Hide the console window on Windows
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        mvn_proc = subprocess.Popen([mvn,'-N','dependency:build-classpath'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo, universal_newlines=True)
        mvn_output, mvn_err = mvn_proc.communicate()
        # print mvn_output
        os.chdir(curdir)
        cp_line = None
        for line in StringIO(mvn_output):
            not_cp_line = non_cp_mvn_output_pattern.match(line)
            if not not_cp_line:
                cp_line = line
                break
        # print '%s -- %s' % (pom_path, cp_line)
        if cp_line:
            jars = cp_line.split(os.pathsep)
            for jar in jars:
                self.classpath.add(jar.strip())
        else:
            print 'WARNING: no classpath found for pom file in path %s' % self.pom_path


'''
PomProjectGeneratorThread: walks a directory tree, searching for all
pom.xml files and generating a project config view result from the findings
'''
class PomProjectGeneratorThread(threading.Thread):
    def __init__(self, target_path, window, long_project_names = False, project_per_pom = False):
        self.target_path = target_path
        self.window = window
        self.project_file_name = os.path.basename(target_path) + '.sublime-project'
        self.long_project_names = long_project_names
        self.project_per_pom = project_per_pom
        self.merged_classpath = set()
        threading.Thread.__init__(self)

    def run(self):
        self.result = None
        pom_paths = []
        os.path.walk(self.target_path, self.find_pom_paths, pom_paths)

        if self.project_per_pom:
            self.result = []
            for pom_path in pom_paths:
                self.result.append({ "folders": [pom_path] })
        else:
            self.result = { "folders": pom_paths }

        cp_threads = []
        finished_cp_threads = []
        max_cp_threads = 4

        if self.project_per_pom:
            for project in self.result:
                # generate project name
                project['folders'][0]['name'] = self.gen_project_name(os.path.join(project['folders'][0]['path'], 'pom.xml'))
                project['folders'][0]['folder_exclude_patterns'] = ['target']
                # grab classpath entries
                cp_thread = MvnClasspathGrabbingThread(project['folders'][0]['path'])
                cp_threads.append(cp_thread)
                cp_thread.start()
                # add pom_path/target/classes to classpath
                project['settings'] = { 'sublimejava_classpath': [
                        os.path.join(project['folders'][0]['path'], 'target', 'classes'),
                        os.path.join(project['folders'][0]['path'], 'target', 'test-classes')
                    ] }
                if len(cp_threads) == max_cp_threads:
                    for cp_thread in cp_threads:
                        cp_thread.join()
                        finished_cp_threads.append(cp_thread)
                    del cp_threads[:]
        else:
            for project_entry in self.result['folders']:
                # generate project name
                project_entry['name'] = self.gen_project_name(os.path.join(project_entry['path'], 'pom.xml'))
                project_entry['folder_exclude_patterns'] = ['target']
                # grab classpath entries
                cp_thread = MvnClasspathGrabbingThread(project_entry['path'])
                cp_threads.append(cp_thread)
                # print 'starting cp thread for %s' % project_entry['path']
                cp_thread.start()
                self.merged_classpath.add(os.path.join(project_entry['path'], 'target', 'classes'))
                self.merged_classpath.add(os.path.join(project_entry['path'], 'target', 'test-classes'))
                if len(cp_threads) == max_cp_threads:
                    for cp_thread in cp_threads:
                        cp_thread.join()
                        self.merged_classpath.update(cp_thread.classpath)
                    del cp_threads[:]

        # print len(cp_threads)
        for cp_thread in cp_threads:
            # print 'waiting on cp_thread'
            cp_thread.join()
            if self.project_per_pom:
                finished_cp_threads.append(cp_thread)
            # print cp_thread.classpath
            self.merged_classpath.update(cp_thread.classpath)

        if not self.project_per_pom:
            self.result['settings'] = { 'sublimejava_classpath': list(self.merged_classpath) }
        else:
            for idx in range(len(self.result)):
                self.result[idx]['settings']['sublimejava_classpath'].extend(finished_cp_threads[idx].classpath)

        # print self.merged_classpath
        sublime.set_timeout(lambda: self.publish_config_view(), 100)

    def gen_project_name(self, pom_path):
        parser = xml.sax.make_parser()
        pom_data = PomHandler()
        parser.setContentHandler(pom_data)
        pom_file = open(pom_path, 'r')
        parser.parse(pom_file)
        pom_file.close()
        return pom_data.get_project_name(self.long_project_names)

    '''
    An os.path.walk() visit function that expects as an arg an empty list.  
    Folder paths are added to the pom_path list
    when a pom.xml file is found (hidden paths and 'target' dirs skipped).
    '''
    def find_pom_paths(self, pom_paths, dirname, names):
        # print project_config
        # print 'dirname=' + dirname
        if 'pom.xml' in names:
            pom_paths.append({ "path": dirname })
        tmpnames = names[:]
        for name in tmpnames:
            # skip hiddens
            if name[0] == '.' or name == 'target':
                names.remove(name)

    def publish_config_view(self):
        if self.project_per_pom:
            for project in self.result:
                project_file_path = os.path.join(project['folders'][0]['path'],
                    os.path.basename(project['folders'][0]['path']) + '.sublime-project')
                project_file = open(project_file_path, 'w+')
                json.dump(project, project_file, indent = 4)
                project_file.close()
        else:
            project_view = self.window.new_file()
            project_edit = project_view.begin_edit()
            project_view.insert(project_edit, 0, json.dumps(self.result, indent = 4))
            project_view.end_edit(project_edit)
            project_view.set_name(self.project_file_name)
            project_view.set_scratch(True)
