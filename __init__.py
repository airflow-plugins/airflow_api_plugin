from airflow.plugins_manager import AirflowPlugin

from airflow_api_plugin.blueprints import airflow_api_blueprint


class AstronomerPlugin(AirflowPlugin):
    name = "airflow_api"

    flask_blueprints = [
        airflow_api_blueprint
    ]
