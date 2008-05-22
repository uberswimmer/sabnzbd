#!/usr/bin/python -OO
# Copyright 2008 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.rss - rss client functionality
"""

__NAME__ = "RSS"


import os
import re
import logging
import time
import sabnzbd
from sabnzbd.interface import ListFilters
from sabnzbd.constants import *
from sabnzbd.decorators import *
from threading import RLock

try:
    import feedparser
    HAVE_FEEDPARSER = True
except ImportError:
    HAVE_FEEDPARSER = False

RE_NEWZBIN = re.compile(r'(newz)(bin|xxx).com/browse/post/(\d+)', re.I)

def ListUris():
    """ Return list of all RSS uris """
    uris = []
    for uri in sabnzbd.CFG['rss']:
        uris.append(uri)
    return uris

def ConvertFilter(text):
    """ Return compiled regex.
        Quote all regex specials, replace '*' by '.*'
    """
    txt = text.replace('\\','\\\\')
    txt = txt.replace('^','\^')
    txt = txt.replace('$','\$')
    txt = txt.replace('.','\.')
    txt = txt.replace('[','\[')
    txt = txt.replace(']','\]')
    txt = txt.replace('(','\(')
    txt = txt.replace(')','\)')
    txt = txt.replace('+','\+')
    txt = txt.replace('?','\?')
    txt = txt.replace('|','\|')
    txt = txt.replace('{','\{')
    txt = txt.replace('}','\}')
    txt = txt.replace('*','.*')

    try:
        return re.compile(txt, re.I)
    except:
        logging.error("[%s] Could not compile regex: %s", __NAME__, text)
        return None


LOCK = RLock()
class RSSQueue:
    def __init__(self):
        self.jobs = {}
        try:
            self.jobs = sabnzbd.load_data(RSS_FILE_NAME, remove = False)
        except:
            pass
        # jobs is a NAME-indexed dictionary
        #    Each element is link-indexed dictionary
        #        Each element is an array:
        #           0 = 'D', 'G', 'B' (downloaded, good-match, bad-match)
        #           1 = Title
        #           2 = URL or MsgId
        #           3 = cat
        #           4 = pp
        #           5 = script
        if type(self.jobs) != type({}):
            self.jobs = {}

        self.shutdown = False
        self.__running = False

    def stop(self):
        self.shutdown = True

    @synchronized(LOCK)
    def run_feed(self, feed=None, download=False):
        """ Run the query for one URI and apply filters """
        self.shutdown = False

        def DupTitle(title):
            for lk in self.jobs:
                if lk[0]=='D' and lk[1]==title:
                    return True
            return False


        if not feed: return

        newlinks = []

        # Preparations, get options
        cfg = sabnzbd.CFG['rss'][feed]
        try:
            uri = cfg['uri']
            defCat = cfg['cat']
            defPP = cfg['pp']
            defScript = cfg['script']
        except:
            logging.error('[%s] Incorrect RSS feed description "%s"', __NAME__, feed)
            return

        # Preparations, convert filters to regex's
        filters = ListFilters(feed)
        regexes = []
        reTypes = []
        reCats = []
        rePPs = []
        reScripts = []
        for n in xrange(len(filters)):
            reCats.append(filters[n][0])
            rePPs.append(filters[n][1])
            reScripts.append(filters[n][2])
            reTypes.append(filters[n][3])
            regexes.append(ConvertFilter(filters[n][4]))
        regcount = len(regexes)

        # Set first if this is the very first scan of this URI
        # in that case nothing will be downloaded.
        first = feed not in self.jobs
        if first:
            self.jobs[feed] = {}

        jobs = self.jobs[feed]

        # Read the RSS feed
        logging.debug("[%s] Running feedparser on %s", __NAME__, uri)
        d = feedparser.parse(uri)
        logging.debug("[%s] Done parsing %s", __NAME__, uri)
        if not d or not d['entries'] or 'bozo_exception' in d:
            logging.warning("[%s] Failed to retrieve RSS from %s", __NAME__, uri)
            return
        entries = d['entries']


        # Filter out valid new links
        for entry in entries:
            if self.shutdown: return

            link = _get_link(uri, entry)

            if link:
                title = entry.title
                newlinks.append(link)

                if DupTitle(title):
                    continue

                myCat = defCat
                myPP = ''
                myScript = ''

                if (link not in jobs) or (jobs[link][0]!='D'):
                    # Match this title against all filters
                    logging.debug('[%s] Trying link %s', __NAME__, link)
                    result = False
                    for n in xrange(regcount):
                        found = re.search(regexes[n], title)
                        if found and reTypes[n]=='A':
                            logging.debug("[%s] Filter matched on rule %d", __NAME__, n)
                            result = True
                            if reCats[n]: myCat = reCats[n]
                            if rePPs[n]: myPP = rePPs[n]
                            if reScripts[n]: myScript = reScripts[n]
                            if not myCat:
                                if not myPP: myPP = defPP
                                if not myScript: myScript = defScript
                            break
                        if found and reTypes[n]=='R':
                            logging.debug("[%s] Filter rejected on rule %d", __NAME__, n)
                            result = False
                            break

                    if result:
                        _HandleLink(jobs, link, title, 'G', myCat, myPP, myScript, download and not first)
                    else:
                        _HandleLink(jobs, link, title, 'B', defCat, defPP, defScript, False)


        # If links were dropped by feed, remove from our tables too
        olds  = jobs.keys()
        for old in olds:
            if old not in newlinks:
                logging.debug("[%s] Purging link %s", __NAME__, old)
                del jobs[old]


    def run(self):
        """ Run all the URI's and filters """
        # Protect against second scheduler call before current
        # run is completed. Cannot use LOCK, because run_feed
        # already uses the LOCK.

        self.shutdown = False
        if not self.__running:
            self.__running = True
            for feed in sabnzbd.CFG['rss']:
                if int(sabnzbd.CFG['rss'][feed]['enable']):
                    self.run_feed(feed, download=True)
                    # Wait two minutes, else sites may get irritated
                    for x in xrange(120):
                        if self.shutdown:
                            self.__running = False
                            return
                        else:
                            time.sleep(1.0)
            self.save()
            self.__running = False


    @synchronized(LOCK)
    def show_result(self, feed):
        if feed in self.jobs:
            try:
                return self.jobs[feed]
            except:
                return {}
        else:
            return {}

    @synchronized(LOCK)
    def save(self):
        sabnzbd.save_data(self.jobs, sabnzbd.RSS_FILE_NAME)

    @synchronized(LOCK)
    def delete(self, feed):
        if feed in self.jobs:
            del self.jobs[feed]

    @synchronized(LOCK)
    def flag_downloaded(self, feed, id):
        if feed in self.jobs:
            lst = self.jobs[feed]
            for link in lst:
                if lst[link][2] == id:
                    lst[link][0] = 'D'


def _HandleLink(jobs, link, title, flag, cat, pp, script, download):
    """ Process one link """
    if script=='': script = None
    if pp=='': pp = None

    m = RE_NEWZBIN.search(link)
    if m and m.group(1).lower() == 'newz' and m.group(2) and m.group(3):
        jobs[link] = []
        if download:
            jobs[link].append('D')
            jobs[link].append(title)
            jobs[link].append('')
            jobs[link].append('')
            jobs[link].append('')
            jobs[link].append('')
            logging.info("[%s] Adding %s (%s) to queue", __NAME__, m.group(3), title)
            sabnzbd.add_msgid(m.group(3), pp=pp, script=script, cat=cat)
        else:
            jobs[link].append(flag)
            jobs[link].append(title)
            jobs[link].append(m.group(3))
            jobs[link].append(cat)
            jobs[link].append(pp)
            jobs[link].append(script)
    else:
        jobs[link] = []
        if download:
            jobs[link].append('D')
            jobs[link].append(title)
            jobs[link].append('')
            jobs[link].append('')
            jobs[link].append('')
            jobs[link].append('')
            logging.info("[%s] Adding %s (%s) to queue", __NAME__, link, title)
            sabnzbd.add_url(link, pp=pp, script=script, cat=cat)
        else:
            jobs[link].append(flag)
            jobs[link].append(title)
            jobs[link].append(link)
            jobs[link].append(cat)
            jobs[link].append(pp)
            jobs[link].append(script)


def _get_link(uri, entry):
    """ Retrieve the post link from this entry """

    uri = uri.lower()
    if uri.find('newzbin') > 0 or uri.find('newzxxx') > 0:
        try:
            link = entry.link
        except:
            link = None
        if not (link and link.lower().find('/post/') > 0):
            # Use alternative link
            try:
                link = entry.links[0].href
            except:
                link = None
    else:
        # Try standard link first
        link = entry.link
        if not link:
            link = entry.links[0].href

    if link and link.lower().find('http') >= 0:
        return link
    else:
        logging.warning('[%s]: Empty RSS entry found (%s)', __NAME__, link)
        return None
