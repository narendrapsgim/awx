import pytest
from unittest import mock

# AWX
from awx.api.serializers import JobTemplateSerializer
from awx.api.versioning import reverse
from awx.main.models import Job, JobTemplate, CredentialType, WorkflowJobTemplate, Organization, Project, Inventory
from awx.main.migrations import _save_password_keys as save_password_keys

# Django
from django.apps import apps
from django.test.utils import override_settings

# DRF
from rest_framework.exceptions import ValidationError

# DAB
from ansible_base.jwt_consumer.common.util import generate_x_trusted_proxy_header
from ansible_base.lib.testing.fixtures import rsa_keypair_factory, rsa_keypair  # noqa: F401; pylint: disable=unused-import


@pytest.mark.django_db
@pytest.mark.parametrize(
    "grant_project, grant_inventory, expect",
    [
        (True, True, 201),
        (True, False, 403),
        (False, True, 403),
    ],
)
def test_create(post, project, machine_credential, inventory, alice, grant_project, grant_inventory, expect):
    if grant_project:
        project.use_role.members.add(alice)
    if grant_inventory:
        inventory.use_role.members.add(alice)
    project.organization.job_template_admin_role.members.add(alice)

    post(
        url=reverse('api:job_template_list'),
        data={'name': 'Some name', 'project': project.id, 'inventory': inventory.id, 'playbook': 'helloworld.yml'},
        user=alice,
        expect=expect,
    )


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ['scm', 'insights'])
def test_invalid_credential_kind_xfail(get, post, organization_factory, job_template_factory, kind):
    objs = organization_factory("org", superusers=['admin'])
    jt = job_template_factory("jt", organization=objs.organization, inventory='test_inv', project='test_proj').job_template

    url = reverse('api:job_template_credentials_list', kwargs={'pk': jt.pk})
    cred_type = CredentialType.defaults[kind]()
    cred_type.save()
    response = post(
        url,
        {
            'name': 'My Cred',
            'credential_type': cred_type.pk,
            'inputs': {
                'username': 'bob',
                'password': 'secret',
            },
        },
        objs.superusers.admin,
        expect=400,
    )
    assert 'Cannot assign a Credential of kind `{}`.'.format(kind) in response.data.values()


@pytest.mark.django_db
def test_create_with_forks_exceeding_maximum_xfail(alice, post, project, inventory, settings):
    project.use_role.members.add(alice)
    inventory.use_role.members.add(alice)
    settings.MAX_FORKS = 10
    response = post(
        url=reverse('api:job_template_list'),
        data={
            'name': 'Some name',
            'project': project.id,
            'inventory': inventory.id,
            'playbook': 'helloworld.yml',
            'forks': 11,
        },
        user=alice,
        expect=400,
    )
    assert 'Maximum number of forks (10) exceeded' in str(response.data)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "grant_project, grant_inventory, expect",
    [
        (True, True, 200),
        (True, False, 403),
        (False, True, 403),
    ],
)
def test_edit_sensitive_fields(patch, job_template_factory, alice, grant_project, grant_inventory, expect):
    objs = job_template_factory('jt', organization='org1', project='prj', inventory='inv', credential='cred')
    objs.job_template.admin_role.members.add(alice)

    if grant_project:
        objs.project.use_role.members.add(alice)
    if grant_inventory:
        objs.inventory.use_role.members.add(alice)

    patch(
        url=reverse('api:job_template_detail', kwargs={'pk': objs.job_template.id}),
        data={
            'name': 'Some name',
            'project': objs.project.id,
            'inventory': objs.inventory.id,
            'playbook': 'alt-helloworld.yml',
        },
        user=alice,
        expect=expect,
    )


@pytest.mark.django_db
def test_reject_dict_extra_vars_patch(patch, job_template_factory, admin_user):
    # Expect a string for extra_vars, raise 400 in this case that would
    # otherwise have been saved incorrectly
    jt = job_template_factory('jt', organization='org1', project='prj', inventory='inv', credential='cred').job_template
    patch(reverse('api:job_template_detail', kwargs={'pk': jt.id}), {'extra_vars': {'foo': 5}}, admin_user, expect=400)


@pytest.mark.django_db
def test_edit_playbook(patch, job_template_factory, alice):
    objs = job_template_factory('jt', organization='org1', project='prj', inventory='inv', credential='cred')
    objs.job_template.admin_role.members.add(alice)
    objs.project.use_role.members.add(alice)
    objs.credential.use_role.members.add(alice)
    objs.inventory.use_role.members.add(alice)

    patch(
        reverse('api:job_template_detail', kwargs={'pk': objs.job_template.id}),
        {
            'playbook': 'alt-helloworld.yml',
        },
        alice,
        expect=200,
    )

    objs.inventory.use_role.members.remove(alice)
    patch(
        reverse('api:job_template_detail', kwargs={'pk': objs.job_template.id}),
        {
            'playbook': 'helloworld.yml',
        },
        alice,
        expect=403,
    )


@pytest.mark.django_db
@pytest.mark.parametrize('json_body', ["abc", True, False, "{\"name\": \"test\"}", 100, 0.5])
def test_invalid_json_body(patch, job_template_factory, alice, json_body):
    objs = job_template_factory('jt', organization='org1')
    objs.job_template.admin_role.members.add(alice)
    resp = patch(reverse('api:job_template_detail', kwargs={'pk': objs.job_template.id}), json_body, alice, expect=400)
    assert resp.data['detail'] == (u'JSON parse error - not a JSON object')


@pytest.mark.django_db
def test_edit_nonsenstive(patch, job_template_factory, alice):
    objs = job_template_factory('jt', organization='org1', project='prj', inventory='inv', credential='cred')
    jt = objs.job_template
    jt.admin_role.members.add(alice)

    res = patch(
        reverse('api:job_template_detail', kwargs={'pk': jt.id}),
        {
            'name': 'updated',
            'description': 'bar',
            'forks': 14,
            'limit': 'something',
            'verbosity': 5,
            'extra_vars': '---',
            'job_tags': 'sometags',
            'force_handlers': True,
            'skip_tags': 'thistag,thattag',
            'ask_variables_on_launch': True,
            'ask_tags_on_launch': True,
            'ask_skip_tags_on_launch': True,
            'ask_job_type_on_launch': True,
            'ask_inventory_on_launch': True,
            'ask_credential_on_launch': True,
        },
        alice,
        expect=200,
    )
    assert res.data['name'] == 'updated'


@pytest.fixture
def jt_copy_edit(job_template_factory, project):
    objects = job_template_factory('copy-edit-job-template', project=project)
    return objects.job_template


@pytest.mark.django_db
def test_job_template_role_user(post, organization_factory, job_template_factory):
    objects = organization_factory("org", superusers=['admin'], users=['test'])

    jt_objects = job_template_factory("jt", organization=objects.organization, inventory='test_inv', project='test_proj')

    url = reverse('api:user_roles_list', kwargs={'pk': objects.users.test.pk})
    response = post(url, dict(id=jt_objects.job_template.execute_role.pk), objects.superusers.admin)
    assert response.status_code == 204


@pytest.mark.django_db
def test_jt_admin_copy_edit_functional(jt_copy_edit, rando, get, post):
    # Grant random user JT admin access only
    jt_copy_edit.admin_role.members.add(rando)
    jt_copy_edit.save()

    get_response = get(reverse('api:job_template_detail', kwargs={'pk': jt_copy_edit.pk}), user=rando)
    assert get_response.status_code == 200

    post_data = get_response.data
    post_data['name'] = '%s @ 12:19:47 pm' % post_data['name']
    post_response = post(reverse('api:job_template_list'), user=rando, data=post_data)
    assert post_response.status_code == 403


@pytest.mark.django_db
def test_launch_with_pending_deletion_inventory(get, post, organization_factory, job_template_factory, machine_credential, credential, net_credential):
    objs = organization_factory("org", superusers=['admin'])
    jt = job_template_factory("jt", organization=objs.organization, credential='c', inventory='test_inv', project='test_proj').job_template
    jt.inventory.pending_deletion = True
    jt.inventory.save()

    resp = post(reverse('api:job_template_launch', kwargs={'pk': jt.pk}), objs.superusers.admin, expect=400)
    assert resp.data['inventory'] == ['The inventory associated with this Job Template is being deleted.']


@pytest.mark.django_db
def test_launch_with_pending_deletion_inventory_workflow(get, post, organization, inventory, admin_user):
    wfjt = WorkflowJobTemplate.objects.create(name='wfjt', organization=organization, inventory=inventory)

    inventory.pending_deletion = True
    inventory.save()

    resp = post(url=reverse('api:workflow_job_template_launch', kwargs={'pk': wfjt.pk}), user=admin_user, expect=400)
    assert resp.data['inventory'] == ['The inventory associated with this Workflow is being deleted.']


@pytest.mark.django_db
def test_jt_without_project(inventory):
    data = dict(name="Test", job_type="run", inventory=inventory.pk, project=None)
    serializer = JobTemplateSerializer(data=data)
    assert not serializer.is_valid()
    assert "project" in serializer.errors
    data["job_type"] = "check"
    serializer = JobTemplateSerializer(data=data)
    assert not serializer.is_valid()
    assert "project" in serializer.errors


@pytest.mark.django_db
def test_disallow_template_delete_on_running_job(job_template_factory, delete, admin_user):
    objects = job_template_factory('jt', credential='c', job_type="run", project='p', inventory='i', organization='o')
    objects.job_template.create_unified_job()
    delete_response = delete(reverse('api:job_template_detail', kwargs={'pk': objects.job_template.pk}), user=admin_user)
    assert delete_response.status_code == 409


@pytest.mark.django_db
def test_save_survey_passwords_to_job(job_template_with_survey_passwords):
    """Test that when a new job is created, the survey_passwords field is
    given all of the passwords that exist in the JT survey"""
    job = job_template_with_survey_passwords.create_unified_job()
    assert job.survey_passwords == {'SSN': '$encrypted$', 'secret_key': '$encrypted$'}


@pytest.mark.django_db
def test_save_survey_passwords_on_migration(job_template_with_survey_passwords):
    """Test that when upgrading to 3.0.2, the jobs connected to a JT that has
    a survey with passwords in it, the survey passwords get saved to the
    job survey_passwords field."""
    Job.objects.create(job_template=job_template_with_survey_passwords)
    save_password_keys.migrate_survey_passwords(apps, None)
    job = job_template_with_survey_passwords.jobs.all()[0]
    assert job.survey_passwords == {'SSN': '$encrypted$', 'secret_key': '$encrypted$'}


@pytest.mark.django_db
def test_jt_organization_follows_project(post, patch, admin_user):
    org1 = Organization.objects.create(name='foo1')
    org2 = Organization.objects.create(name='foo2')
    project_common = dict(scm_type='git', playbook_files=['helloworld.yml'])
    project1 = Project.objects.create(name='proj1', organization=org1, **project_common)
    project2 = Project.objects.create(name='proj2', organization=org2, **project_common)
    r = post(
        url=reverse('api:job_template_list'),
        data={"name": "fooo", "ask_inventory_on_launch": True, "project": project1.pk, "playbook": "helloworld.yml"},
        user=admin_user,
        expect=201,
    )
    data = r.data
    assert data['organization'] == project1.organization_id
    data['project'] = project2.id
    jt = JobTemplate.objects.get(pk=data['id'])
    r = patch(url=jt.get_absolute_url(), data=data, user=admin_user, expect=200)
    assert r.data['organization'] == project2.organization_id


@pytest.mark.django_db
def test_jt_organization_field_is_read_only(patch, post, project, admin_user):
    org = project.organization
    jt = JobTemplate.objects.create(name='foo_jt', ask_inventory_on_launch=True, project=project, playbook='helloworld.yml')
    org2 = Organization.objects.create(name='foo2')
    r = patch(url=jt.get_absolute_url(), data={'organization': org2.id}, user=admin_user, expect=200)
    assert r.data['organization'] == org.id
    assert JobTemplate.objects.get(pk=jt.pk).organization == org

    # similar test, but on creation
    r = post(
        url=reverse('api:job_template_list'),
        data={'name': 'foobar', 'project': project.id, 'organization': org2.id, 'ask_inventory_on_launch': True, 'playbook': 'helloworld.yml'},
        user=admin_user,
        expect=201,
    )
    assert r.data['organization'] == org.id
    assert JobTemplate.objects.get(pk=r.data['id']).organization == org


@pytest.mark.django_db
def test_callback_disallowed_null_inventory(project):
    jt = JobTemplate.objects.create(name='test-jt', inventory=None, ask_inventory_on_launch=True, project=project, playbook='helloworld.yml')
    serializer = JobTemplateSerializer(jt)
    assert serializer.instance == jt
    with pytest.raises(ValidationError) as exc:
        serializer.validate({'host_config_key': 'asdfbasecfeee'})
    assert 'Cannot enable provisioning callback without an inventory set' in str(exc)


@pytest.mark.django_db
def test_job_template_branch_error(project, inventory, post, admin_user):
    r = post(
        url=reverse('api:job_template_list'),
        data={"name": "fooo", "inventory": inventory.pk, "project": project.pk, "playbook": "helloworld.yml", "scm_branch": "foobar"},
        user=admin_user,
        expect=400,
    )
    assert 'Project does not allow overriding branch' in str(r.data['scm_branch'])


@pytest.mark.django_db
def test_job_template_branch_prompt_error(project, inventory, post, admin_user):
    r = post(
        url=reverse('api:job_template_list'),
        data={"name": "fooo", "inventory": inventory.pk, "project": project.pk, "playbook": "helloworld.yml", "ask_scm_branch_on_launch": True},
        user=admin_user,
        expect=400,
    )
    assert 'Project does not allow overriding branch' in str(r.data['ask_scm_branch_on_launch'])


@pytest.mark.django_db
def test_job_template_missing_inventory(project, inventory, admin_user, post):
    jt = JobTemplate.objects.create(
        name='test-jt', inventory=inventory, ask_inventory_on_launch=True, project=project, playbook='helloworld.yml', host_config_key='abcd'
    )
    Inventory.objects.get(pk=inventory.pk).delete()
    r = post(
        url=reverse('api:job_template_callback', kwargs={'pk': jt.pk}),
        data={'host_config_key': 'abcd'},
        user=admin_user,
        expect=400,
    )
    assert r.status_code == 400
    assert "Cannot start automatically, an inventory is required." in str(r.data)


@pytest.mark.django_db
class TestJobTemplateCallbackProxyIntegration:
    """
    Test the interaction of provision job template callback feature and:
        settings.PROXY_IP_ALLOWED_LIST
        x-trusted-proxy http header
    """

    @pytest.fixture
    def job_template(self, inventory, project):
        jt = JobTemplate.objects.create(name='test-jt', inventory=inventory, project=project, playbook='helloworld.yml', host_config_key='abcd')
        return jt

    @override_settings(REMOTE_HOST_HEADERS=['HTTP_X_FROM_THE_LOAD_BALANCER', 'REMOTE_ADDR', 'REMOTE_HOST'], PROXY_IP_ALLOWED_LIST=['my.proxy.example.org'])
    def test_host_not_found(self, job_template, admin_user, post, rsa_keypair):  # noqa: F811
        job_template.inventory.hosts.create(name='foobar')

        headers = {
            'HTTP_X_FROM_THE_LOAD_BALANCER': 'baz',
            'REMOTE_HOST': 'baz',
            'REMOTE_ADDR': 'baz',
        }
        r = post(
            url=reverse('api:job_template_callback', kwargs={'pk': job_template.pk}), data={'host_config_key': 'abcd'}, user=admin_user, expect=400, **headers
        )
        assert r.data['msg'] == 'No matching host could be found!'

    @pytest.mark.parametrize(
        'headers, expected',
        (
            pytest.param(
                {
                    'HTTP_X_FROM_THE_LOAD_BALANCER': 'foobar',
                    'REMOTE_HOST': 'my.proxy.example.org',
                },
                201,
            ),
            pytest.param(
                {
                    'HTTP_X_FROM_THE_LOAD_BALANCER': 'foobar',
                    'REMOTE_HOST': 'not-my-proxy.org',
                },
                400,
            ),
        ),
    )
    @override_settings(REMOTE_HOST_HEADERS=['HTTP_X_FROM_THE_LOAD_BALANCER', 'REMOTE_ADDR', 'REMOTE_HOST'], PROXY_IP_ALLOWED_LIST=['my.proxy.example.org'])
    def test_proxy_ip_allowed_list(self, job_template, admin_user, post, headers, expected):  # noqa: F811
        job_template.inventory.hosts.create(name='my.proxy.example.org')

        post(
            url=reverse('api:job_template_callback', kwargs={'pk': job_template.pk}),
            data={'host_config_key': 'abcd'},
            user=admin_user,
            expect=expected,
            **headers
        )

    @override_settings(REMOTE_HOST_HEADERS=['HTTP_X_FROM_THE_LOAD_BALANCER', 'REMOTE_ADDR', 'REMOTE_HOST'], PROXY_IP_ALLOWED_LIST=[])
    def test_no_proxy_trust_all_headers(self, job_template, admin_user, post):
        job_template.inventory.hosts.create(name='foobar')

        headers = {
            'HTTP_X_FROM_THE_LOAD_BALANCER': 'foobar',
            'REMOTE_ADDR': 'bar',
            'REMOTE_HOST': 'baz',
        }
        post(url=reverse('api:job_template_callback', kwargs={'pk': job_template.pk}), data={'host_config_key': 'abcd'}, user=admin_user, expect=201, **headers)

    @override_settings(REMOTE_HOST_HEADERS=['HTTP_X_FROM_THE_LOAD_BALANCER', 'REMOTE_ADDR', 'REMOTE_HOST'], PROXY_IP_ALLOWED_LIST=['my.proxy.example.org'])
    def test_trusted_proxy(self, job_template, admin_user, post, rsa_keypair):  # noqa: F811
        job_template.inventory.hosts.create(name='foobar')

        headers = {
            'HTTP_X_TRUSTED_PROXY': generate_x_trusted_proxy_header(rsa_keypair.private),
            'HTTP_X_FROM_THE_LOAD_BALANCER': 'foobar, my.proxy.example.org',
        }

        with mock.patch('ansible_base.jwt_consumer.common.cache.JWTCache.get_key_from_cache', lambda self: None):
            with override_settings(ANSIBLE_BASE_JWT_KEY=rsa_keypair.public):
                post(
                    url=reverse('api:job_template_callback', kwargs={'pk': job_template.pk}),
                    data={'host_config_key': 'abcd'},
                    user=admin_user,
                    expect=201,
                    **headers
                )

    @override_settings(REMOTE_HOST_HEADERS=['HTTP_X_FROM_THE_LOAD_BALANCER', 'REMOTE_ADDR', 'REMOTE_HOST'], PROXY_IP_ALLOWED_LIST=['my.proxy.example.org'])
    def test_trusted_proxy_host_not_found(self, job_template, admin_user, post, rsa_keypair):  # noqa: F811
        job_template.inventory.hosts.create(name='foobar')

        headers = {
            'HTTP_X_TRUSTED_PROXY': generate_x_trusted_proxy_header(rsa_keypair.private),
            'HTTP_X_FROM_THE_LOAD_BALANCER': 'baz, my.proxy.example.org',
            'REMOTE_ADDR': 'bar',
            'REMOTE_HOST': 'baz',
        }

        with mock.patch('ansible_base.jwt_consumer.common.cache.JWTCache.get_key_from_cache', lambda self: None):
            with override_settings(ANSIBLE_BASE_JWT_KEY=rsa_keypair.public):
                post(
                    url=reverse('api:job_template_callback', kwargs={'pk': job_template.pk}),
                    data={'host_config_key': 'abcd'},
                    user=admin_user,
                    expect=400,
                    **headers
                )
