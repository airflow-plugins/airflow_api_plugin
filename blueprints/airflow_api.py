from datetime import datetime
import json
import os
import six
import time
import logging

from flask import Blueprint, request, Response
from sqlalchemy import or_
from airflow import settings
from airflow.exceptions import AirflowException, AirflowConfigException
from airflow.models import DagBag, DagRun, DagModel
from airflow.utils.state import State
from airflow.utils.dates import date_range as utils_date_range
from airflow.www.app import csrf
from airflow.bin.cli import pause, unpause, get_dag as cli_get_dag, connections as cli_connections

airflow_api_blueprint = Blueprint('airflow_api', __name__, url_prefix='/api/v1')


class ApiInputException(Exception):
    pass


class ApiResponse:

    def __init__(self):
        pass

    STATUS_OK = 200
    STATUS_BAD_REQUEST = 400
    STATUS_UNAUTHORIZED = 401
    STATUS_NOT_FOUND = 404
    STATUS_SERVER_ERROR = 500

    @staticmethod
    def standard_response(status, payload):
        json_data = json.dumps({
            'response': payload
        })
        resp = Response(json_data, status=status, mimetype='application/json')
        return resp

    @staticmethod
    def success(payload):
        return ApiResponse.standard_response(ApiResponse.STATUS_OK, payload)

    @staticmethod
    def error(status, error):
        return ApiResponse.standard_response(status, {
            'error': error
        })

    @staticmethod
    def bad_request(error):
        return ApiResponse.error(ApiResponse.STATUS_BAD_REQUEST, error)

    @staticmethod
    def not_found(error='Resource not found'):
        return ApiResponse.error(ApiResponse.STATUS_NOT_FOUND, error)

    @staticmethod
    def unauthorized(error='Not authorized to access this resource'):
        return ApiResponse.error(ApiResponse.STATUS_UNAUTHORIZED, error)

    @staticmethod
    def server_error(error='An unexpected problem occurred'):
        return ApiResponse.error(ApiResponse.STATUS_SERVER_ERROR, error)


class dagCliArgs:
    def __init__(self, dag_id, subdir):
        self.subdir = subdir
        self.dag_id = dag_id
        


@airflow_api_blueprint.before_request
def verify_authentication():
    authorization = request.headers.get('authorization')
    try:
        api_auth_key = settings.conf.get('AIRFLOW_API_PLUGIN', 'AIRFLOW_API_AUTH')
    except AirflowConfigException:
        return

    if authorization != api_auth_key:
        return ApiResponse.unauthorized("You are not authorized to use this resource")


def format_dag_run(dag_run):
    return {
        'run_id': dag_run.run_id,
        'dag_id': dag_run.dag_id,
        'state': dag_run.get_state(),
        'start_date': (None if not dag_run.start_date else str(dag_run.start_date)),
        'end_date': (None if not dag_run.end_date else str(dag_run.end_date)),
        'external_trigger': dag_run.external_trigger,
        'execution_date': str(dag_run.execution_date)
    }


def find_dag_runs(session, dag_id, dag_run_id, execution_date):
    qry = session.query(DagRun)
    qry = qry.filter(DagRun.dag_id == dag_id)
    qry = qry.filter(or_(DagRun.run_id == dag_run_id, DagRun.execution_date == execution_date))

    return qry.order_by(DagRun.execution_date).all()


@airflow_api_blueprint.route('/dags', methods=['GET'])
def dags_index():
    dagbag = DagBag('dags')
    dags = []
    for dag_id in dagbag.dags:
        payload = {
            'dag_id': dag_id,
            'full_path': None,
            'is_active': False,
            'last_execution': None,
        }

        dag = dagbag.get_dag(dag_id)

        if dag:
            payload['full_path'] = dag.full_filepath
            payload['is_active'] = (not dag.is_paused)
            payload['last_execution'] = str(dag.latest_execution_date)

        dags.append(payload)

    return ApiResponse.success({'dags': dags})

@csrf.exempt
@airflow_api_blueprint.route('/dags/<dag_id>', methods=['PUT'])
def dag_update(dag_id):
    args = dagCliArgs(dag_id, 'dags')
    try:
        dag = cli_get_dag(args)
    except AirflowException:
        return ApiResponse.not_found('Could not find a dag with ID {}'.format(dag_id))
    logging.info("Processing dag {} PUT body {}".format(dag_id, request.get_json()))
    body = request.get_json()
    if body is None or 'is_active' not in body:
        return ApiResponse.bad_request("A Json body with 'is_active': True/False is expected")

    try:
        if body['is_active']:
            unpause(None, dag)
        elif not body['is_active']:
            pause(None, dag)
    except AirflowException:
        return ApiResponse.not_found('Could not pause/unpause dag with ID {}'.format(dag_id))

    payload = {
        'dag_id': dag_id,
        'full_path': dag.full_filepath,
        'is_active': (not dag.is_paused),
        'last_execution': str(dag.latest_execution_date)
    }

    return ApiResponse.success(payload)


@airflow_api_blueprint.route('/dags/<dag_id>', methods=['GET'])
def get_dag(dag_id):
    args = dagCliArgs(dag_id, 'dags')

    try:
        dag = cli_get_dag(args)
    except AirflowException:
        return ApiResponse.not_found('Could not find a dag with ID {}'.format(dag_id))

    payload = {
        'dag_id': dag_id,
        'full_path': dag.full_filepath,
        'is_active': (not dag.is_paused),
        'last_execution': str(dag.latest_execution_date)
    }

    return ApiResponse.success(payload)


@airflow_api_blueprint.route('/dag_runs', methods=['GET'])
def get_dag_runs():
    dag_runs = []

    session = settings.Session()

    query = session.query(DagRun)

    if request.args.get('state') is not None:
        query = query.filter(DagRun.state == request.args.get('state'))

    if request.args.get('external_trigger') is not None:
        # query = query.filter(DagRun.external_trigger == (request.args.get('external_trigger') is True))
        query = query.filter(DagRun.external_trigger == (request.args.get('external_trigger') in ['true', 'True']))

    if request.args.get('prefix') is not None:
        query = query.filter(DagRun.run_id.ilike('{}%'.format(request.args.get('prefix'))))

    runs = query.order_by(DagRun.execution_date).all()

    for run in runs:
        dag_runs.append(format_dag_run(run))

    session.close()

    return ApiResponse.success({'dag_runs': dag_runs})


@csrf.exempt
@airflow_api_blueprint.route('/dag_runs', methods=['POST'])
def create_dag_run():
    # decode input
    data = request.get_json(force=True)

    # ensure there is a dag id
    if 'dag_id' not in data or data['dag_id'] is None:
        return ApiResponse.bad_request('Must specify the dag id to create dag runs for')
    dag_id = data['dag_id']

    limit = 500
    partial = False
    if 'limit' in data and data['limit'] is not None:
        try:
            limit = int(data['limit'])
            if limit <= 0:
                return ApiResponse.bad_request('Limit must be a number greater than 0')
            if limit > 500:
                return ApiResponse.bad_request('Limit cannot exceed 500')
        except ValueError:
            return ApiResponse.bad_request('Limit must be an integer')

    if 'partial' in data and data['partial'] in ['true', 'True', True]:
        partial = True

    # ensure there is run data
    start_date = datetime.now()
    end_date = datetime.now()

    if 'start_date' in data and data['start_date'] is not None:
        try:
            start_date = datetime.strptime(data['start_date'], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            error = '\'start_date\' has invalid format \'{}\', Ex format: YYYY-MM-DDThh:mm:ss'
            return ApiResponse.bad_request(error.format(data['start_date']))

    if 'end_date' in data and data['end_date'] is not None:
        try:
            end_date = datetime.strptime(data['end_date'], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            error = '\'end_date\' has invalid format \'{}\', Ex format: YYYY-MM-DDThh:mm:ss'
            return ApiResponse.bad_request(error.format(data['end_date']))

    # determine run_id prefix
    prefix = 'manual_{}'.format(int(time.time()))
    if 'prefix' in data and data['prefix'] is not None:
        prefix = data['prefix']

        if 'backfill' in prefix:
            return ApiResponse.bad_request('Prefix cannot contain \'backfill\', Airflow will ignore dag runs using it')
        # ensure prefix doesn't have an underscore appended
        if prefix[:-1:] == "_":
            prefix = prefix[:-1]

    conf = None
    if 'conf' in data and data['conf'] is not None:
        if isinstance(data['conf'], six.string_types):
            conf = data['conf']
        else:
            try:
                conf = json.dumps(data['conf'])
            except Exception:
                return ApiResponse.bad_request('Could not encode specified conf JSON')

    try:
        session = settings.Session()

        dagbag = DagBag('dags')

        if dag_id not in dagbag.dags:
            return ApiResponse.bad_request("Dag id {} not found".format(dag_id))

        dag = dagbag.get_dag(dag_id)

        # ensure run data has all required attributes and that everything is valid, returns transformed data
        runs = utils_date_range(start_date=start_date, end_date=end_date, delta=dag._schedule_interval)

        if len(runs) > limit and partial is False:
            error = '{} dag runs would be created, which exceeds the limit of {}.' \
                    ' Reduce start/end date to reduce the dag run count'
            return ApiResponse.bad_request(error.format(len(runs), limit))

        payloads = []
        for exec_date in runs:
            run_id = '{}_{}'.format(prefix, exec_date.isoformat())

            if find_dag_runs(session, dag_id, run_id, exec_date):
                continue

            payloads.append({
                'run_id': run_id,
                'execution_date': exec_date,
                'conf': conf
            })

        results = []
        for index, run in enumerate(payloads):
            if len(results) >= limit:
                break

            dag.create_dagrun(
                run_id=run['run_id'],
                execution_date=run['execution_date'],
                state=State.RUNNING,
                conf=conf,
                external_trigger=True
            )
            results.append(run['run_id'])

        session.close()
    except ApiInputException as e:
        return ApiResponse.bad_request(str(e))
    except ValueError as e:
        return ApiResponse.server_error(str(e))
    except AirflowException as e:
        return ApiResponse.server_error(str(e))
    except Exception as e:
        return ApiResponse.server_error(str(e))

    return ApiResponse.success({'dag_run_ids': results})


@airflow_api_blueprint.route('/dag_runs/<dag_run_id>', methods=['GET'])
def get_dag_run(dag_run_id):
    session = settings.Session()

    runs = DagRun.find(run_id=dag_run_id, session=session)

    if len(runs) == 0:
        return ApiResponse.not_found('Dag run not found')

    dag_run = runs[0]

    session.close()

    return ApiResponse.success({'dag_run': format_dag_run(dag_run)})

class connectionCliArgs:
    def __init__(self, mode, conn_id=None, conn_uri=None ):
        
        self.list = False
        self.delete = False
        self.add = False

        if mode == 'list':
            self.list = True
        elif mode == 'delete':
            self.delete = True
        elif mode == 'add':
            self.add = True
        
        self.conn_id = conn_id
        self.conn_uri = conn_uri
        self.conn_extra = None
        self.conn_type = None
        self.conn_host = None
        self.conn_login = None
        self.conn_password = None
        self.conn_schema = None
        self.conn_port = None

@csrf.exempt
@airflow_api_blueprint.route('/connections/<conn_id>', methods=['DELETE'])
def delete_connections(conn_id):
    args = connectionCliArgs('delete',conn_id=conn_id)

    try:
        cli_connections(args)
    except AirflowException:
        return ApiResponse.error('Could not delete the connection')

    payload = {
        'status': 'deleted'
    }

    return ApiResponse.success(payload)

@csrf.exempt
@airflow_api_blueprint.route('/connections', methods=['POST'])
def add_connections():

    # decode input
    data = request.get_json(force=True)
    # ensure there is a conn_id
    if 'conn_id' not in data or data['conn_id'] is None:
        return ApiResponse.bad_request('Must specify the connection id (conn_id) for the new connection')
    conn_id = data['conn_id']

    # ensure there is a dag id
    if 'conn_uri' not in data or data['conn_uri'] is None:
        return ApiResponse.bad_request('Must specify the connection uri (conn_uri) for the new connection')
    conn_uri = data['conn_uri']

    args = connectionCliArgs('add',conn_id=conn_id, conn_uri=conn_uri)

    try:
        cli_connections(args)
    except AirflowException:
        return ApiResponse.error('Could not add the new connection ')

    payload = {
        'status': 'created'
    }

    return ApiResponse.success(payload)
