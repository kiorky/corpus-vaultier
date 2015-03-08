#!/usr/bin/env python
# -*- coding: utf-8 -*-
__docformat__ = 'restructuredtext en'

'''

Vaultier ldap account synchronizator
'''

import sys
import os
import re
import logging
from contextlib import contextmanager
import ldap
from collections import OrderedDict

_marker = object()
_HANDLERS = {}

log = logging.getLogger('vaultier_ldap_import')


def stripfilter(string):
    return re.sub(
        '  ',
        '',
        string.replace('\n', '')
    ).replace(
        ') (', ')(').replace(
            '(& (', '(&(').strip()


class _ConnectionHandler(object):

    def __init__(self,
                 uri,
                 base=None,
                 user=None,
                 password=None,
                 tls=None,
                 retrieve_attributes=None,
                 scope=None):
        if tls is None:
            tls = True
        if not scope:
            scope = 'subtree'
        self.scope = getattr(ldap, 'SCOPE_{0}'.format(scope.upper()),
                             'SCOPE_SUBTREE')
        self.tls = tls
        if retrieve_attributes is None:
            retrieve_attributes = []
        self.retrieve_attributes = retrieve_attributes
        self.uri = uri
        self.user = user
        self.password = password
        self.conns = OrderedDict()
        self.base = base

    def query(self,
              search,
              retrieve_attributes=None,
              conn=None,
              base=None,
              scope=None):
        results = None
        if conn is None:
            conn = self.connect()
        if base is None:
            base = self.base
        if scope is None:
            scope = self.scope
        if isinstance(scope, basestring):
            scope = {'ONE': 'ONELEVEL'}.get(scope.upper(), scope.upper())
            scope = getattr(ldap, 'SCOPE_{0}'.format(scope.upper()))
        if retrieve_attributes is None:
            retrieve_attributes = self.retrieve_attributes
        if base is None:
            raise ValueError('Please select a base')
        if not search:
            search = 'objectClass=top'
        results = conn.search_st(base,
                                 scope,
                                 search,
                                 retrieve_attributes,
                                 timeout=60)
        return results

    def unbind(self, disconnect=None):
        if isinstance(disconnect, tuple):
            disconnect = [disconnect]
        elif disconnect is None:
            disconnect = [a for a in self.conns]
        for connid in disconnect:
            try:
                self.conns[connid].unbind()
            except:
                pass
            self.conns.pop(connid, None)

    def connect(self):
        conn = self.conns.get((self.uri, self.user), _marker)
        if conn is _marker:
            conn = ldap.initialize(self.uri)
            if self.tls:
                conn.start_tls_s()
            if self.user:
                conn.simple_bind_s(self.user, self.password)
            else:
                conn.simple_bind()
            self.conns[(self.uri, self.user)] = conn
        return conn


@contextmanager
def get_handler(uri, **ckw):
    '''
    Helper to handle a pool of ldap connexion and gracefully disconnects

    This use the previous _ConnectionHandler on the behalf of a connexion
    manager to handle gracefully connection and deconnection.

      uri
        ldap url
      base
        base to search
      user
        user dn
      password
        password
      tls
        activate tls encryption
      retrieve_attributes
        default query retrieved attributes
      scope
        default query scope

    ::

       >>> with get_handler("ldap://ldap.foo.net",
                           base="dc=foo,dc=org",
                           user="uid=xxx,ou=People,dc=x",
                                password="xxx") as h:
       ...     h.query('objectClass=person')
       ...     h.query('objectClass=groupOfNames')
       >>>

    '''
    kw = {}
    for i in [a for a in ckw if not a.startswith('__')]:
        kw[i] = ckw[i]
    handler = _HANDLERS.get(uri, _marker)
    if handler is _marker:
        _HANDLERS[uri] = _ConnectionHandler(uri, **kw)
    try:
        yield _HANDLERS[uri]
    finally:
        _HANDLERS[uri].unbind()
        _HANDLERS.pop(uri, None)


def run(exit=True, vaultier_install=None):
    '''

    Wrap everything on the run block to run dinamically
    from another project as vaultier code
    '''
    # load django
    d = os.path.abspath(os.path.dirname(__file__))
    if not vaultier_install:
        vaultier_install = os.path.join(d, 'vaultier/vaultier')
    sys.path.append(vaultier_install)
    try:
        import django
        django.setup()
    except Exception:
        pass
    from acls.business.fields import RoleLevelField
    from django.db.models.loading import get_model
    from accounts.business.fields import MemberStatusField
    from django.conf import settings
    Member = get_model('accounts', 'Member')
    Vault = get_model('vaults', 'Vault')
    Card = get_model('cards', 'Card')
    User = get_model('accounts', 'User')
    Role = get_model('acls', 'Role')
    Workspace = get_model('workspaces', 'Workspace')
    Members = Member.objects
    Vaults = Vault.objects
    Cards = Card.objects
    Roles = Role.objects
    logging.basicConfig(
        format="%(asctime)-15s %(name)s - %(message)s",
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S')
    log.info('start')
    if not getattr(settings, 'MC_LDAP_IMPORT', None):
        raise Exception('import not enabled')
    if getattr(settings, 'MC_LDAP_DEBUG', None):
        ldap.set_option(ldap.OPT_DEBUG_LEVEL, 999999999)
    uri = settings.MC_LDAP_IMPORT_URL.strip()
    cdn = settings.MC_LDAP_DN.strip()
    admin_id = getattr(settings, 'MC_LDAP_ADMIN_ID', 1)
    base = settings.MC_LDAP_BASE.strip()
    password = settings.MC_LDAP_PASSWORD.strip()
    lfilter = settings.MC_LDAP_FILTER.strip()
    ws = settings.MC_LDAP_DEFAULT_WORKSPACE.strip()
    workspace = Workspace.objects.filter(slug=ws).first()
    if not workspace:
        raise Exception('{0} worspace does not exists'.format(ws))
    # get ldap users
    results = None
    with get_handler(
        uri, base=base, user=cdn, password=password
    ) as handler:
        results = handler.query(lfilter)
    errors = []
    admin = User.objects.filter(id=admin_id)[0]
    usersinfos = {}
    for dn, result in results:
        done = False
        mails = result.get('mail', [])
        if not mails:
            continue
        uid = result['uid'][0]
        mail, longmail = None, None
        _maile, domain = mails[0].split('@')
        if len('_mail') in [3, 4, 5] and ('.' not in _maile):
            mail = mails[0]
        else:
            longmail = mails[0]
        if 'gosaMailAlternateAddress' in result:
            # makina custom
            if longmail:
                mail = '{0}@{1}'.format(result['uid'][0], domain)
            elif mail:
                longmail = '{r[givenName][0]}.{r[sn][0]}@{domain}'.format(
                    r=result, domain=domain).lower()
        else:
            longmail = mail
        # search or create related user
        user, results = None, []
        results.extend(
            User.objects.filter(email__in=[longmail, mail]).all())
        results.extend(
            User.objects.filter(nickname__in=[uid]).all())
        if results:
            user = results[0]
        if not user:
            # creation
            user = User(nickname=uid, email=mail)
        if not user:
            # creation
            errors.append(' - {0}: user not selected'.format(uid))
            continue
        save = False
        if user.nickname != uid or user.email != mail:
            save = True
        user.nickname = uid
        user.email = mail
        if save:
            user.save()
            done = True
        # invite user to workspace
        default_status = MemberStatusField.STATUS_MEMBER_WITHOUT_WORKSPACE_KEY
        default_acl = RoleLevelField.LEVEL_CREATE
        member = Members.get_concrete_member_to_workspace(workspace, user)
        if not member:
            member = Member(workspace=workspace,
                            user=user,
                            invitation_email=mail,
                            status=default_status,
                            created_by=admin)
            member.save()
            done = True
        existing_top_roles = Roles.filter(
            to_workspace=workspace, to_card=None, to_vault=None, member=member)
        existing_roles = Roles.filter(
            to_workspace=workspace, member=member)
        if not existing_top_roles:
            Roles.create_or_update_role(
                Role(member=member,
                     to_workspace=workspace,
                     created_by=admin,
                     level=default_acl))
            Members.remove_role_duplicatates(member)
            done = True
        usersinfos[uid] = {'user': user,
                           'top_roles': existing_top_roles,
                           'roles': existing_roles,
                           'member': member}
        done = done and 'imported' or 'existing'
        log.info('  - {0} ({1}) {2}'.format(uid, mail, done))
    for vaults in getattr(settings, 'MC_LDAP_VAULTS', []):
        for vault, vaultdata in vaults.items():
            vmembers = []
            for accesses in vaultdata.get('access', []):
                for vaccesslevel, vadata in accesses.items():
                    results = None
                    with get_handler(
                        uri, base=base, user=cdn, password=password
                    ) as handler:
                        lfilter = stripfilter(vadata['filter'])
                        try:
                            results = handler.query(lfilter)
                        except:
                            errors.append('vault {0}; invalid'
                                          ' filter'.format(vault))
                            continue
                    if not results:
                        errors.append(
                            'Vault filter failed {0} vault'.format(vault))
                        continue
                    ovault = None
                    try:
                        ovault = Vaults.filter(slug=vault)[0]
                    except IndexError:
                        ovault = Vaults.filter(name__icontains=vault)[0]
                    if not ovault:
                        errors.append('Missing {0} vault'.format(vault))
                        continue
                    lvl = {'ADMIN': 'WRITE',
                           'MANAGE': 'WRITE'}.get(vaccesslevel.upper(),
                                                  vaccesslevel.upper())
                    vilvl = getattr(RoleLevelField, 'LEVEL_' + lvl,
                                    default_acl)
                    for dn, result in results:
                        uid = result['uid'][0]
                        if uid not in usersinfos:
                            errors.append('No info for {0}'.format(uid))
                            continue
                        user = usersinfos[uid]['user']
                        member = usersinfos[uid]['member']
                        vmembers.append(member)
                        vrole = Roles.filter(to_vault=ovault,
                                             member=member).all()
                        if vrole:
                            vrole = vrole[0]
                            if vrole.level != vilvl:
                                vrole = None
                        if not vrole:
                            log.info('Vault {0}: join; {1}'.format(vault, uid))
                            vrole = Roles.create_or_update_role(
                                Role(member=member,
                                     to_vault=ovault,
                                     created_by=admin,
                                     level=vilvl))
                            if vrole.level != vilvl:
                                vrole.level = vilvl
                                vrole.save()
            dvto_delete = [role
                           for role in Roles.filter(to_vault=ovault)
                           if role.member not in vmembers]
            for dvrole in dvto_delete:
                log.info(
                    'vault {0}: revoking access for {1}'.format(
                        role.to_vault.name,
                        member.user.nickname))
                dvrole.delete()
            for cards in vaultdata.get('cards', []):
                for card, carddata in cards.items():
                    cmembers = []
                    for accesses in carddata.get('access', []):
                        for caccesslevel, cadata in accesses.items():
                            cresults = None
                            with get_handler(
                                uri, base=base, user=cdn, password=password
                            ) as handler:
                                lfilter = stripfilter(cadata['filter'])
                                cresults = handler.query(lfilter)
                            if not cresults:
                                errors.append(
                                    'Card filter failed  {0} card'.format(
                                        card))
                                continue
                            ocard = None
                            try:
                                ocard = Cards.filter(
                                    slug=card, vault=ovault)[0]
                            except IndexError:
                                try:
                                    ocard = Cards.filter(
                                        name__icontains=card, vault=ovault)[0]
                                except IndexError:
                                    try:
                                        ocard = Cards.filter(
                                            slug__icontains=card,
                                            vault=ovault)[0]
                                    except:
                                        ocard = None
                            if not ocard:
                                errors.append(
                                    'CARD: Missing {0} card'.format(card))
                                continue
                            clvl = {'ADMIN': 'WRITE',
                                    'MANAGE': 'WRITE'}.get(
                                        caccesslevel.upper(),
                                        caccesslevel.upper())
                            cilvl = getattr(RoleLevelField, 'LEVEL_' + clvl,
                                            default_acl)
                            for dn, cresult in cresults:
                                uid = cresult['uid'][0]
                                if uid not in usersinfos:
                                    errors.append(
                                        'CARD: No info for {0}'.format(uid))
                                    continue
                                user = usersinfos[uid]['user']
                                member = usersinfos[uid]['member']
                                cmembers.append(member)
                                crole = Roles.filter(to_vault=ovault,
                                                     member=member).all()
                                if crole:
                                    crole = crole[0]
                                    if crole.level != cilvl:
                                        crole = None
                                if not crole:
                                    log.info(
                                        'Card {0}: join; {1}'.format(
                                            card, uid))
                                    crole = Roles.create_or_update_role(
                                        Role(member=member,
                                             to_card=ocard,
                                             created_by=admin,
                                             level=cilvl))
                                    if crole.level != cilvl:
                                        crole.level = cilvl
                                        crole.save()
                    dcto_delete = [role
                                   for role in Roles.filter(to_card=ocard)
                                   if role.member not in cmembers]
                    for dcrole in dcto_delete:
                        log.info(
                            'card {0}: revoking access for {1}'.format(
                                role.to_card.name,
                                member.user.nickname))
                        dcrole.delete()

    ret = 0
    if errors:
        for i in errors:
            log.error('ERRORS\n')
            log.error(i)
            ret = 1
    if exit:
        sys.exit(ret)


if __name__ == '__main__':
    run()
# vim:set et sts=4 ts=4 tw=80: