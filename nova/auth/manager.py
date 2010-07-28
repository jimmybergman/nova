# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Nova authentication management
"""

import logging
import os
import shutil
import string
import sys
import tempfile
import uuid
import zipfile

from nova import crypto
from nova import datastore
from nova import exception
from nova import flags
from nova import objectstore # for flags
from nova import utils
from nova.auth import ldapdriver # for flags
from nova.auth import signer

FLAGS = flags.FLAGS

# NOTE(vish): a user with one of these roles will be a superuser and
#             have access to all api commands
flags.DEFINE_list('superuser_roles', ['cloudadmin'],
                  'Roles that ignore rbac checking completely')

# NOTE(vish): a user with one of these roles will have it for every
#             project, even if he or she is not a member of the project
flags.DEFINE_list('global_roles', ['cloudadmin', 'itsec'],
                  'Roles that apply to all projects')


flags.DEFINE_bool('use_vpn', True, 'Support per-project vpns')
flags.DEFINE_string('credentials_template',
                    utils.abspath('auth/novarc.template'),
                    'Template for creating users rc file')
flags.DEFINE_string('vpn_client_template',
                    utils.abspath('cloudpipe/client.ovpn.template'),
                    'Template for creating users vpn file')
flags.DEFINE_string('credential_key_file', 'pk.pem',
                    'Filename of private key in credentials zip')
flags.DEFINE_string('credential_cert_file', 'cert.pem',
                    'Filename of certificate in credentials zip')
flags.DEFINE_string('credential_rc_file', 'novarc',
                    'Filename of rc in credentials zip')

flags.DEFINE_integer('vpn_start_port', 1000,
                    'Start port for the cloudpipe VPN servers')
flags.DEFINE_integer('vpn_end_port', 2000,
                    'End port for the cloudpipe VPN servers')

flags.DEFINE_string('credential_cert_subject',
                    '/C=US/ST=California/L=MountainView/O=AnsoLabs/'
                    'OU=NovaDev/CN=%s-%s',
                    'Subject for certificate for users')

flags.DEFINE_string('vpn_ip', '127.0.0.1',
                    'Public IP for the cloudpipe VPN servers')

flags.DEFINE_string('auth_driver', 'nova.auth.ldapdriver.FakeLdapDriver',
                    'Driver that auth manager uses')

class AuthBase(object):
    """Base class for objects relating to auth

    Objects derived from this class should be stupid data objects with
    an id member. They may optionally contain methods that delegate to
    AuthManager, but should not implement logic themselves.
    """
    @classmethod
    def safe_id(cls, obj):
        """Safe get object id

        This method will return the id of the object if the object
        is of this class, otherwise it will return the original object.
        This allows methods to accept objects or ids as paramaters.

        """
        if isinstance(obj, cls):
            return obj.id
        else:
            return obj


class User(AuthBase):
    """Object representing a user"""
    def __init__(self, id, name, access, secret, admin):
        self.id = id
        self.name = name
        self.access = access
        self.secret = secret
        self.admin = admin

    def is_superuser(self):
        return AuthManager().is_superuser(self)

    def is_admin(self):
        return AuthManager().is_admin(self)

    def has_role(self, role):
        return AuthManager().has_role(self, role)

    def add_role(self, role):
        return AuthManager().add_role(self, role)

    def remove_role(self, role):
        return AuthManager().remove_role(self, role)

    def is_project_member(self, project):
        return AuthManager().is_project_member(self, project)

    def is_project_manager(self, project):
        return AuthManager().is_project_manager(self, project)

    def generate_key_pair(self, name):
        return AuthManager().generate_key_pair(self.id, name)

    def create_key_pair(self, name, public_key, fingerprint):
        return AuthManager().create_key_pair(self.id,
                                             name,
                                             public_key,
                                             fingerprint)

    def get_key_pair(self, name):
        return AuthManager().get_key_pair(self.id, name)

    def delete_key_pair(self, name):
        return AuthManager().delete_key_pair(self.id, name)

    def get_key_pairs(self):
        return AuthManager().get_key_pairs(self.id)

    def __repr__(self):
        return "User('%s', '%s', '%s', '%s', %s)" % (self.id,
                                                     self.name,
                                                     self.access,
                                                     self.secret,
                                                     self.admin)


class KeyPair(AuthBase):
    """Represents an ssh key returned from the datastore

    Even though this object is named KeyPair, only the public key and
    fingerprint is stored. The user's private key is not saved.
    """
    def __init__(self, id, name, owner_id, public_key, fingerprint):
        self.id = id
        self.name = name
        self.owner_id = owner_id
        self.public_key = public_key
        self.fingerprint = fingerprint

    def __repr__(self):
        return "KeyPair('%s', '%s', '%s', '%s', '%s')" % (self.id,
                                                          self.name,
                                                          self.owner_id,
                                                          self.public_key,
                                                          self.fingerprint)


class Project(AuthBase):
    """Represents a Project returned from the datastore"""
    def __init__(self, id, name, project_manager_id, description, member_ids):
        self.id = id
        self.name = name
        self.project_manager_id = project_manager_id
        self.description = description
        self.member_ids = member_ids

    @property
    def project_manager(self):
        return AuthManager().get_user(self.project_manager_id)

    @property
    def vpn_ip(self):
        ip, port = AuthManager().get_project_vpn_data(self)
        return ip

    @property
    def vpn_port(self):
        ip, port = AuthManager().get_project_vpn_data(self)
        return port

    def has_manager(self, user):
        return AuthManager().is_project_manager(user, self)

    def has_member(self, user):
        return AuthManager().is_project_member(user, self)

    def add_role(self, user, role):
        return AuthManager().add_role(user, role, self)

    def remove_role(self, user, role):
        return AuthManager().remove_role(user, role, self)

    def has_role(self, user, role):
        return AuthManager().has_role(user, role, self)

    def get_credentials(self, user):
        return AuthManager().get_credentials(user, self)

    def __repr__(self):
        return "Project('%s', '%s', '%s', '%s', %s)" % (self.id,
                                                        self.name,
                                                        self.project_manager_id,
                                                        self.description,
                                                        self.member_ids)


class NoMorePorts(exception.Error):
    pass


class Vpn(datastore.BasicModel):
    """Manages vpn ips and ports for projects"""
    def __init__(self, project_id):
        self.project_id = project_id
        super(Vpn, self).__init__()

    @property
    def identifier(self):
        """Identifier used for key in redis"""
        return self.project_id

    @classmethod
    def create(cls, project_id):
        """Creates a vpn for project

        This method finds a free ip and port and stores the associated
        values in the datastore.
        """
        # TODO(vish): get list of vpn ips from redis
        port = cls.find_free_port_for_ip(FLAGS.vpn_ip)
        vpn = cls(project_id)
        # save ip for project
        vpn['project'] = project_id
        vpn['ip'] = FLAGS.vpn_ip
        vpn['port'] = port
        vpn.save()
        return vpn

    @classmethod
    def find_free_port_for_ip(cls, ip):
        """Finds a free port for a given ip from the redis set"""
        # TODO(vish): these redis commands should be generalized and
        #             placed into a base class. Conceptually, it is
        #             similar to an association, but we are just
        #             storing a set of values instead of keys that
        #             should be turned into objects.
        redis = datastore.Redis.instance()
        key = 'ip:%s:ports' % ip
        # TODO(vish): these ports should be allocated through an admin
        #             command instead of a flag
        if (not redis.exists(key) and
            not redis.exists(cls._redis_association_name('ip', ip))):
            for i in range(FLAGS.vpn_start_port, FLAGS.vpn_end_port + 1):
                redis.sadd(key, i)

        port = redis.spop(key)
        if not port:
            raise NoMorePorts()
        return port

    @classmethod
    def num_ports_for_ip(cls, ip):
        """Calculates the number of free ports for a given ip"""
        return datastore.Redis.instance().scard('ip:%s:ports' % ip)

    @property
    def ip(self):
        """The ip assigned to the project"""
        return self['ip']

    @property
    def port(self):
        """The port assigned to the project"""
        return int(self['port'])

    def save(self):
        """Saves the association to the given ip"""
        self.associate_with('ip', self.ip)
        super(Vpn, self).save()

    def destroy(self):
        """Cleans up datastore and adds port back to pool"""
        self.unassociate_with('ip', self.ip)
        datastore.Redis.instance().sadd('ip:%s:ports' % self.ip, self.port)
        super(Vpn, self).destroy()


class AuthManager(object):
    """Manager Singleton for dealing with Users, Projects, and Keypairs

    Methods accept objects or ids.

    AuthManager uses a driver object to make requests to the data backend.
    See ldapdriver for reference.

    AuthManager also manages associated data related to Auth objects that
    need to be more accessible, such as vpn ips and ports.
    """
    _instance=None
    def __new__(cls, *args, **kwargs):
        """Returns the AuthManager singleton with driver set

        __init__ is run every time AuthManager() is called, so we need to do
        any constructor related stuff here. The driver that is specified
        in the flagfile is loaded here.
        """
        if not cls._instance:
            cls._instance = super(AuthManager, cls).__new__(
                    cls, *args, **kwargs)
            mod_str, sep, driver_str = FLAGS.auth_driver.rpartition('.')
            try:
                __import__(mod_str)
                cls._instance.driver = getattr(sys.modules[mod_str],
                                               driver_str)
            except (ImportError, AttributeError):
                raise exception.Error('Auth driver %s cannot be found'
                                      % FLAGS.auth_driver)
        return cls._instance

    def authenticate(self, access, signature, params, verb='GET',
                     server_string='127.0.0.1:8773', path='/',
                     check_type='ec2', headers=None):
        """Authenticates AWS request using access key and signature

        If the project is not specified, attempts to authenticate to
        a project with the same name as the user. This way, older tools
        that have no project knowledge will still work.

        @type access: str
        @param access: Access key for user in the form "access:project".

        @type signature: str
        @param signature: Signature of the request.

        @type params: list of str
        @param params: Web paramaters used for the signature.

        @type verb: str
        @param verb: Web request verb ('GET' or 'POST').

        @type server_string: str
        @param server_string: Web request server string.

        @type path: str
        @param path: Web request path.

        @type check_type: str
        @param check_type: Type of signature to check. 'ec2' for EC2, 's3' for
                           S3. Any other value will cause signature not to be
                           checked.

        @type headers: list
        @param headers: HTTP headers passed with the request (only needed for
                        s3 signature checks)

        @rtype: tuple (User, Project)
        @return: User and project that the request represents.
        """
        # TODO(vish): check for valid timestamp
        (access_key, sep, project_id) = access.partition(':')

        logging.info('Looking up user: %r', access_key)
        user = self.get_user_from_access_key(access_key)
        logging.info('user: %r', user)
        if user == None:
            raise exception.NotFound('No user found for access key %s' %
                                     access_key)

        # NOTE(vish): if we stop using project name as id we need better
        #             logic to find a default project for user
        if project_id is '':
            project_id = user.name

        project = self.get_project(project_id)
        if project == None:
            raise exception.NotFound('No project called %s could be found' %
                                     project_id)
        if not self.is_admin(user) and not self.is_project_member(user,
                                                                  project):
            raise exception.NotFound('User %s is not a member of project %s' %
                                     (user.id, project.id))
        if check_type == 's3':
            expected_signature = signer.Signer(user.secret.encode()).s3_authorization(headers, verb, path)
            logging.debug('user.secret: %s', user.secret)
            logging.debug('expected_signature: %s', expected_signature)
            logging.debug('signature: %s', signature)
            if signature != expected_signature:
                raise exception.NotAuthorized('Signature does not match')
        elif check_type == 'ec2':
            # NOTE(vish): hmac can't handle unicode, so encode ensures that
            #             secret isn't unicode
            expected_signature = signer.Signer(user.secret.encode()).generate(
                    params, verb, server_string, path)
            logging.debug('user.secret: %s', user.secret)
            logging.debug('expected_signature: %s', expected_signature)
            logging.debug('signature: %s', signature)
            if signature != expected_signature:
                raise exception.NotAuthorized('Signature does not match')
        return (user, project)

    def is_superuser(self, user):
        """Checks for superuser status, allowing user to bypass rbac

        @type user: User or uid
        @param user: User to check.

        @rtype: bool
        @return: True for superuser.
        """
        if not isinstance(user, User):
            user = self.get_user(user)
        # NOTE(vish): admin flag on user represents superuser
        if user.admin:
            return True
        for role in FLAGS.superuser_roles:
            if self.has_role(user, role):
                return True

    def is_admin(self, user):
        """Checks for admin status, allowing user to access all projects

        @type user: User or uid
        @param user: User to check.

        @rtype: bool
        @return: True for admin.
        """
        if not isinstance(user, User):
            user = self.get_user(user)
        if self.is_superuser(user):
            return True
        for role in FLAGS.global_roles:
            if self.has_role(user, role):
                return True

    def has_role(self, user, role, project=None):
        """Checks existence of role for user

        If project is not specified, checks for a global role. If project
        is specified, checks for the union of the global role and the
        project role.

        Role 'projectmanager' only works for projects and simply checks to
        see if the user is the project_manager of the specified project. It
        is the same as calling is_project_manager(user, project).

        @type user: User or uid
        @param user: User to check.

        @type role: str
        @param role: Role to check.

        @type project: Project or project_id
        @param project: Project in which to look for local role.

        @rtype: bool
        @return: True if the user has the role.
        """
        with self.driver() as drv:
            if role == 'projectmanager':
                if not project:
                    raise exception.Error("Must specify project")
                return self.is_project_manager(user, project)

            global_role = drv.has_role(User.safe_id(user),
                                        role,
                                        None)
            if not global_role:
                return global_role

            if not project or role in FLAGS.global_roles:
                return global_role

            return drv.has_role(User.safe_id(user),
                                 role,
                                 Project.safe_id(project))

    def add_role(self, user, role, project=None):
        """Adds role for user

        If project is not specified, adds a global role. If project
        is specified, adds a local role.

        The 'projectmanager' role is special and can't be added or removed.

        @type user: User or uid
        @param user: User to which to add role.

        @type role: str
        @param role: Role to add.

        @type project: Project or project_id
        @param project: Project in which to add local role.
        """
        with self.driver() as drv:
            drv.add_role(User.safe_id(user), role, Project.safe_id(project))

    def remove_role(self, user, role, project=None):
        """Removes role for user

        If project is not specified, removes a global role. If project
        is specified, removes a local role.

        The 'projectmanager' role is special and can't be added or removed.

        @type user: User or uid
        @param user: User from which to remove role.

        @type role: str
        @param role: Role to remove.

        @type project: Project or project_id
        @param project: Project in which to remove local role.
        """
        with self.driver() as drv:
            drv.remove_role(User.safe_id(user), role, Project.safe_id(project))

    def get_project(self, pid):
        """Get project object by id"""
        with self.driver() as drv:
            project_dict = drv.get_project(pid)
            if project_dict:
                return Project(**project_dict)

    def get_projects(self):
        """Retrieves list of all projects"""
        with self.driver() as drv:
            project_list = drv.get_projects()
            if not project_list:
                return []
            return [Project(**project_dict) for project_dict in project_list]

    def create_project(self, name, manager_user,
                       description=None, member_users=None):
        """Create a project

        @type name: str
        @param name: Name of the project to create. The name will also be
        used as the project id.

        @type manager_user: User or uid
        @param manager_user: This user will be the project manager.

        @type description: str
        @param project: Description of the project. If no description is
        specified, the name of the project will be used.

        @type member_users: list of User or uid
        @param: Initial project members. The project manager will always be
        added as a member, even if he isn't specified in this list.

        @rtype: Project
        @return: The new project.
        """
        if member_users:
            member_users = [User.safe_id(u) for u in member_users]
        with self.driver() as drv:
            project_dict =  drv.create_project(name,
                                               User.safe_id(manager_user),
                                               description,
                                               member_users)
            if project_dict:
                if FLAGS.use_vpn:
                    Vpn.create(project_dict['id'])
                return Project(**project_dict)

    def add_to_project(self, user, project):
        """Add user to project"""
        with self.driver() as drv:
            return drv.add_to_project(User.safe_id(user),
                                       Project.safe_id(project))

    def is_project_manager(self, user, project):
        """Checks if user is project manager"""
        if not isinstance(project, Project):
            project = self.get_project(project)
        return User.safe_id(user) == project.project_manager_id

    def is_project_member(self, user, project):
        """Checks to see if user is a member of project"""
        if not isinstance(project, Project):
            project = self.get_project(project)
        return User.safe_id(user) in project.member_ids

    def remove_from_project(self, user, project):
        """Removes a user from a project"""
        with self.driver() as drv:
            return drv.remove_from_project(User.safe_id(user),
                                            Project.safe_id(project))

    def get_project_vpn_data(self, project):
        """Gets vpn ip and port for project

        @type project: Project or project_id
        @param project: Project from which to get associated vpn data

        @rvalue: tuple of (str, str)
        @return: A tuple containing (ip, port) or None, None if vpn has
        not been allocated for user.
        """
        vpn = Vpn.lookup(Project.safe_id(project))
        if not vpn:
            return None, None
        return (vpn.ip, vpn.port)

    def delete_project(self, project):
        """Deletes a project"""
        with self.driver() as drv:
            return drv.delete_project(Project.safe_id(project))

    def get_user(self, uid):
        """Retrieves a user by id"""
        with self.driver() as drv:
            user_dict = drv.get_user(uid)
            if user_dict:
                return User(**user_dict)

    def get_user_from_access_key(self, access_key):
        """Retrieves a user by access key"""
        with self.driver() as drv:
            user_dict = drv.get_user_from_access_key(access_key)
            if user_dict:
                return User(**user_dict)

    def get_users(self):
        """Retrieves a list of all users"""
        with self.driver() as drv:
            user_list = drv.get_users()
            if not user_list:
                return []
            return [User(**user_dict) for user_dict in user_list]

    def create_user(self, name, access=None, secret=None, admin=False):
        """Creates a user

        @type name: str
        @param name: Name of the user to create.

        @type access: str
        @param access: Access Key (defaults to a random uuid)

        @type secret: str
        @param secret: Secret Key (defaults to a random uuid)

        @type admin: bool
        @param admin: Whether to set the admin flag. The admin flag gives
        superuser status regardless of roles specifed for the user.

        @type create_project: bool
        @param: Whether to create a project for the user with the same name.

        @rtype: User
        @return: The new user.
        """
        if access == None: access = str(uuid.uuid4())
        if secret == None: secret = str(uuid.uuid4())
        with self.driver() as drv:
            user_dict = drv.create_user(name, access, secret, admin)
            if user_dict:
                return User(**user_dict)

    def delete_user(self, user):
        """Deletes a user"""
        with self.driver() as drv:
            drv.delete_user(User.safe_id(user))

    def generate_key_pair(self, user, key_name):
        """Generates a key pair for a user

        Generates a public and private key, stores the public key using the
        key_name, and returns the private key and fingerprint.

        @type user: User or uid
        @param user: User for which to create key pair.

        @type key_name: str
        @param key_name: Name to use for the generated KeyPair.

        @rtype: tuple (private_key, fingerprint)
        @return: A tuple containing the private_key and fingerprint.
        """
        # NOTE(vish): generating key pair is slow so check for legal
        #             creation before creating keypair
        uid = User.safe_id(user)
        with self.driver() as drv:
            if not drv.get_user(uid):
                raise exception.NotFound("User %s doesn't exist" % user)
            if drv.get_key_pair(uid, key_name):
                raise exception.Duplicate("The keypair %s already exists"
                                          % key_name)
        private_key, public_key, fingerprint = crypto.generate_key_pair()
        self.create_key_pair(uid, key_name, public_key, fingerprint)
        return private_key, fingerprint

    def create_key_pair(self, user, key_name, public_key, fingerprint):
        """Creates a key pair for user"""
        with self.driver() as drv:
            kp_dict =  drv.create_key_pair(User.safe_id(user),
                                           key_name,
                                           public_key,
                                           fingerprint)
            if kp_dict:
                return KeyPair(**kp_dict)

    def get_key_pair(self, user, key_name):
        """Retrieves a key pair for user"""
        with self.driver() as drv:
            kp_dict = drv.get_key_pair(User.safe_id(user), key_name)
            if kp_dict:
                return KeyPair(**kp_dict)

    def get_key_pairs(self, user):
        """Retrieves all key pairs for user"""
        with self.driver() as drv:
            kp_list = drv.get_key_pairs(User.safe_id(user))
            if not kp_list:
                return []
            return [KeyPair(**kp_dict) for kp_dict in kp_list]

    def delete_key_pair(self, user, key_name):
        """Deletes a key pair for user"""
        with self.driver() as drv:
            drv.delete_key_pair(User.safe_id(user), key_name)

    def get_credentials(self, user, project=None):
        """Get credential zip for user in project"""
        if not isinstance(user, User):
            user = self.get_user(user)
        if project is None:
            project = user.id
        pid = Project.safe_id(project)
        rc = self.__generate_rc(user.access, user.secret, pid)
        private_key, signed_cert = self._generate_x509_cert(user.id, pid)

        vpn = Vpn.lookup(pid)
        if not vpn:
            raise exception.Error("No vpn data allocated for project %s" %
                                  project.name)
        configfile = open(FLAGS.vpn_client_template,"r")
        s = string.Template(configfile.read())
        configfile.close()
        config = s.substitute(keyfile=FLAGS.credential_key_file,
                              certfile=FLAGS.credential_cert_file,
                              ip=vpn.ip,
                              port=vpn.port)

        tmpdir = tempfile.mkdtemp()
        zf = os.path.join(tmpdir, "temp.zip")
        zippy = zipfile.ZipFile(zf, 'w')
        zippy.writestr(FLAGS.credential_rc_file, rc)
        zippy.writestr(FLAGS.credential_key_file, private_key)
        zippy.writestr(FLAGS.credential_cert_file, signed_cert)
        zippy.writestr("nebula-client.conf", config)
        zippy.writestr(FLAGS.ca_file, crypto.fetch_ca(user.id))
        zippy.close()
        with open(zf, 'rb') as f:
            buffer = f.read()

        shutil.rmtree(tmpdir)
        return buffer

    def __generate_rc(self, access, secret, pid):
        """Generate rc file for user"""
        rc = open(FLAGS.credentials_template).read()
        rc = rc % { 'access': access,
                    'project': pid,
                    'secret': secret,
                    'ec2': FLAGS.ec2_url,
                    's3': 'http://%s:%s' % (FLAGS.s3_host, FLAGS.s3_port),
                    'nova': FLAGS.ca_file,
                    'cert': FLAGS.credential_cert_file,
                    'key': FLAGS.credential_key_file,
            }
        return rc

    def _generate_x509_cert(self, uid, pid):
        """Generate x509 cert for user"""
        (private_key, csr) = crypto.generate_x509_cert(
                self.__cert_subject(uid))
        # TODO(joshua): This should be async call back to the cloud controller
        signed_cert = crypto.sign_csr(csr, pid)
        return (private_key, signed_cert)

    def __cert_subject(self, uid):
        """Helper to generate cert subject"""
        return FLAGS.credential_cert_subject % (uid, utils.isotime())