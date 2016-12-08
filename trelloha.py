#!/usr/bin/env python
import json
import logging
import netrc
import os
import re

import defusedxml.ElementTree
import requests
import trello

TRELLO_APP_KEY = "ec65a98b933f15b1fdb63dd79ef281b3"

LOG = logging.getLogger(__name__)


class NoAuth(RuntimeError):
    def __init__(self, trello):
        super(NoAuth, self).__init__(
            """No authentication token found or token expired.\n
Go to:\n%s\n\nand add the following to your ~/.netrc file:\n
machine trello.com login <BOARD_ID> password <TOKEN>""" %
            trello.get_token_url("Trelloha",
                                 expires='never',
                                 write_access=True))


class Trelloha(object):

    CERTS = {'https://code.engineering.redhat.com': 'rh-cacert.crt'}
    GERRIT_URLS = {"OpenStack": "https://review.openstack.org",
                   "RDO": "https://review.rdoproject.org",
                   "RHOS": "https://code.engineering.redhat.com/gerrit"}
    BUGZILLA_URLS = {"Red Hat": "https://bugzilla.redhat.com"}

    GITHUB_URL = "https://github.com"

    def __init__(self):
        self.trello = trello.TrelloApi(TRELLO_APP_KEY)
        self.board_id, token = self.get_board_token()
        self.trello.set_token(token)

    def get_board_token(self, site_name="trello.com", netrc_file=None):
        """Read a .netrc file and return login/password."""
        n = netrc.netrc(netrc_file)
        if site_name not in n.hosts:
            raise NoAuth(self.trello)
        return n.hosts[site_name][0], n.hosts[site_name][2]

    def get_verify(self, url):
        for baseurl, cert in self.CERTS.items():
            if url.startswith(baseurl):
                return os.path.join(os.path.dirname(__file__), cert)
        return True

    # TODO(jd) add that in trello.boards
    def checkitem_update_state(self, card_id, checklist_id, checkitem_id,
                               state):
        resp = requests.put(
            "https://trello.com/1/cards/%s/checklist/%s/checkItem/%s/state"
            % (card_id, checklist_id, checkitem_id),
            params=dict(key=self.trello._apikey, token=self.trello._token),
            data=dict(value=state))
        resp.raise_for_status()
        return json.loads(resp.content)

    def get_review(self, gerrit_url, review_id):
        r = requests.get("%s/changes/%d" % (gerrit_url, review_id),
                         verify=self.get_verify(gerrit_url))
        return json.loads(r.text[5:])

    def get_bugzilla(self, bugzilla_url, bug_id):
        r = requests.get("%s/show_bug.cgi?ctype=xml&id=%s" % (bugzilla_url,
                                                              bug_id),
                         verify=self.get_verify(bugzilla_url))
        return defusedxml.ElementTree.fromstring(r.content)

    def is_a_github_pull_request_merged(self, checklist_item):
        if self.GITHUB_URL not in checklist_item['name']:
            return False
        matched = re.search("%s/(.*)/(pull|issue)/(\d+)" % self.GITHUB_URL,
                            checklist_item['name'])
        if not matched:
            return False
        repo = matched.group(1)
        kind = matched.group(2)
        pr_id = int(matched.group(3))
        r = requests.get("https://api.github.com/repos/%s/%ss/%s" %
                         (repo, kind, pr_id))
        info = json.loads(r.text)
        if info['state'] in ['closed', 'merged']:
            LOG.info("Github %s %s %s is %s" %
                     (repo, kind, pr_id, info['state']))
            return True
        return False

    def is_a_gerrit_review_merged(self, checklist_item):
        for gerrit_name, gerrit_url in self.GERRIT_URLS.items():
            if gerrit_url in checklist_item['name']:
                break
        else:
            return False
        matched = re.search("%s/(#/c/)?(\d+)" % gerrit_url,
                            checklist_item['name'])
        if not matched:
            return False

        review = self.get_review(gerrit_url, int(matched.group(2)))
        merged = review['status'] == "MERGED"
        if merged:
            LOG.info("%s review %s is merged" % (gerrit_name, review['id']))
        return merged

    def is_a_bugzilla_modified(self, checklist_item):
        for bugzilla_name, bugzilla_url in self.BUGZILLA_URLS.items():
            if bugzilla_url in checklist_item['name']:
                break
        else:
            return False

        matched = re.search("%s/show_bug.cgi\?id=(\d+)" % bugzilla_url,
                            checklist_item['name'])
        if not matched:
            return False
        bug_id = int(matched.group(1))
        bugzilla = self.get_bugzilla(bugzilla_url, bug_id)
        bug = bugzilla.find('bug')
        if "error" in bug.attrib:
            LOG.debug("Not allowed to view BZ%s: %s" %
                      (bug_id, bug.attrib["error"]))
            bug_status = None
        else:
            bug_status = bugzilla.find('bug/bug_status').text

        if bug_status in ["MODIFIED", "ON_QA", "VERIFIED", "RELEASING_PENDING",
                          "CLOSED"]:
            LOG.info("%s bugzilla %s is %s" % (bugzilla_name, bug_id,
                                               bug_status))
            return True
        return False

    def update_trello_card_checklist_with_review(self):
        try:
            for card in self.trello.boards.get_card(self.board_id,
                                                    filter="visible",
                                                    checklists="all"):
                for checklist in card["checklists"]:
                    for item in checklist['checkItems']:
                        completed = (
                            item['state'] == "incomplete" and
                            (self.is_a_gerrit_review_merged(item) or
                             self.is_a_github_pull_request_merged(item) or
                             self.is_a_bugzilla_modified(item))
                        )
                        if completed:
                            LOG.info("Setting %s to complete" % item['id'])
                            self.checkitem_update_state(checklist['idCard'],
                                                        checklist['id'],
                                                        item['id'],
                                                        "complete")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                raise NoAuth(self.trello)
            raise


def main():
    t = Trelloha()
    t.update_trello_card_checklist_with_review()


if __name__ == '__main__':
    main()
