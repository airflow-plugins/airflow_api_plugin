from datetime import datetime
import json
import six
import time

from flask import Blueprint, request, Response, jsonify, url_for
from sqlalchemy import or_
from airflow import settings
from airflow.exceptions import AirflowException, AirflowConfigException
from airflow.models import DagBag, DagRun, DagModel
from airflow.utils.state import State
from airflow.utils.dates import date_range as utils_date_range
from airflow.www.app import csrf

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
@airflow_api_blueprint.route('/dags/<string:dag_id>/paused/<string:paused>',
                             methods=['POST'])
def dag_paused(dag_id, paused):
    """(Un)pauses a dag"""

    session = settings.Session()
    orm_dag = (
        session.query(DagModel)
               .filter(DagModel.dag_id == dag_id).first()
    )
    if paused == 'true':
        orm_dag.is_paused = True
    else:
        orm_dag.is_paused = False

    session.merge(orm_dag)
    session.commit()
    session.close()

    return ApiResponse.success({dag_id: paused})


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


@airflow_api_blueprint.route('/latest_runs', methods=['GET'])
def latest_dag_runs():
    """Returns the latest DagRun for each DAG formatted for the UI. """
    dagruns = DagRun.get_latest_runs()
    payload = []
    for dagrun in dagruns:
        if dagrun.execution_date:
            payload.append({
                'dag_id': dagrun.dag_id,
                'execution_date': dagrun.execution_date.isoformat(),
                'start_date': ((dagrun.start_date or '') and
                               dagrun.start_date.isoformat()),
                'dag_run_url': url_for('airflow.graph',
                                       dag_id=dagrun.dag_id,
                                       execution_date=dagrun.execution_date)
            })
    return ApiResponse.success(items=payload)
