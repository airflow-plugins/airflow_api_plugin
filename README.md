***NOTICE: This plugin will not work if you are using an executor plugin***

# Airflow Plugin - API

This plugin exposes REST-like endpoints for to perform operations and access Airflow data.
Airflow itself doesn't abstract any logic into reusable components so
this API will replicate application logic. Due to how this is done it is possible
that the API will have behavior differences from UI.


# Troubleshooting
## The scheduler fails to start when using this plugin
This has been experienced when Airflow is configured to use a plugin executor.
For CSRF this plugin imports `airflow/www/app.py`, which happen to also import `airflow/jobs`.
When the scheduler starts, it will import `airflow/jobs` which will import the plugin
executor.  This creates a circular import, crashing the scheduler.  At Astronomer we fixed
this in our fork by moving the CSRF instantation to its own file that `airflow/www/app.py` imports,
however that isn't in stock Airflow, therefore this is being documented as a known issue.





# API Reference


## General Info

### Authentication
By default authentication is disabled.  Currently only basic token authentication is supported.

Authentication can be enabled either via:

**Config**

```
[airflow_api_plugin]
airflow_api_auth=**secret token**
```

**Environment variable**

`AIRFLOW__AIRFLOW__API__PLUGIN_AIRFLOW_API_AUTH=**secret token**`

Once configured, a request simply has to pass an  `authorization` header with the value being the secret.
ie. `authorization: **secret token**`. If the config/env is set and the header doesn't match, the request will be denied

### Body
Data sent in the body of a POST request will need to be formatted as raw JSON, not multipart or form data.

#### Correct:
```
POST  `/api/v1/dag_runs`
{"dag_id":"example_dag","prefix":"prefix_value"}
```

#### Incorrect:
```
POST  `/api/v1/dag_runs`
dag_id=example_dag&prefix=prefixvalue
```


### Responses
All responses will have a json object with a `response` key, and a value that is always an object. ie:
```
{
  "response": { /* payload of data */ }
}
```
#### Success
Success responses will vary depending on the endpoint. See the example success responses below for the specifics of the response values

#### Error
All error response values will be of the form
```
{
  "response": {
    "error": "Error message"
  }
}
```

## Endpoints

### Dag Endpoints

#### List
Returns a list of dags currently in the DagBag along with additional information like the active status and last execution time

**Request**

`GET` `/api/v1/dags`


**Parameters**

None

**Success Response**

```
{
  "response": {
    "dags": [
      {
        "is_active": true,
        "dag_id": "hello",
        "full_path": "dags/hello.py",
        "last_execution": "2017-09-19 12:00:00"
      },
      ...
    ]
  }
}
```

### Dag Run Endpoints

#### List
Returns a list of dag runs, up to 100 per request.  It can be filtered by a start run id, state, and/or prefix.

**Request**

`GET` `/api/v1/dag_runs`

**Parameters**

| Key              | Method | Required? | Type    | Default | Descripton                                                                                                                   |
|------------------|--------|-----------|---------|---------|------------------------------------------------------------------------------------------------------------------------------|
| start            | Query  | No        | String  | 0       | The dag run id to start returning results for, exclusive of the id itself                                                    |
| limit            | Query  | No        | Integer | 50      | The limit to the number of results to return in a single request                                                             |
| state            | Query  | No        | String  |         | Filter dag runs by a specified state                                                                                         |
| external_trigger | Query  | No        | Boolean |         | Filter dag runs by whether or not they were triggered internally (ie by the scheduler) or externally (ie this API or the CLI |
| prefix           | Query  | No        | String  |         | Filter dag runs to only the runs with a `run_id` containing the full prefix specified                                        |
| dag_id           | Query  | No        | String  |         | Filter dag runs to only the runs with `dag_id`                                                                               |

**Success Response**

```
{
  "response": {
    "dag_runs": [
      {
        "external_trigger": "False",
        "execution_date": "2017-09-19 12:00:00",
        "end_date": "None",
        "start_date": "2017-09-20 18:10:02.190813",
        "state": "success",
        "run_id": "scheduled__2017-09-19T12:00:00"
      },
      ...
    ]
  }
}
```

#### Create
Creates a dag run object for each run specified in the payload.  Dag runs will be added to the database and executed by the scheduler at some point in the future. The resulting dag run id's will be return on success.

The prefix pattern allows for groups of dag run to be created and queried later on.  This is useful in backfill operations as they will happen asynchronously
Each dag run created will increment a number after the prefix, followed by the start date of the run.

**Request**

`POST` `/api/v1/dag_runs`

**Parameters**

| Key        | Method | Required? | Type          | Default | Descripton                                                                                                                                                                        |
|------------|--------|-----------|---------------|---------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| dag_id     | Post   | Yes       | String        |         | The dag id of the dag the dag runs should be created for                                                                                                                          |
| prefix     | Post   | Yes       | String        | manual_ | A prefix used when generating the run_id of the dag run. Prefix cannot contain the word "backfill"                                                                                |
| start_date | Post   | No        | String        | NOW()   | The start date where this call should start creating dag runs from (inclusive)                                                                                                    |
| end_date   | Post   | No        | String        | NOW()   | The end date where this call should stop creating dag runs at (inclusive)                                                                                                         |
| limit      | Post   | No        | Integer       | 500     | The maximum number of dag runs to create in a single request. Maximum is 500                                                                                                      |
| partial    | Post   | No        | Boolean       | false   | Whether or not a partial batch can be created. If true, and the number of dag runs that would be created between the start and end exceeds the limit, no dag runs will be created |
| conf       | Post   | No        | String, JSON  |         | JSON configuration added to the DagRun's conf attribute                                                                                                                           |

**Success Response**

```
{
  "response": {
    "dag_run_ids": [
      "prefix_0000001_2017-09-01T12:00:00",
      "prefix_0000002_2017-09-02T12:00:00",
      "prefix_0000003_2017-09-03T12:00:00",
      ...
      "prefix_00000010_2017-09-10T12:00:00"
    ]
  }
}
```

#### Get
Returns information on a specific dag run

**Request**

`GET` `/api/v1/dag_runs/<dag_run_id>`

**Parameters**

None

**Success Response**

```
{
  "response": {
    "dag_run": {
      "end_date": "None",
      "external_trigger": "False",
      "state": "success",
      "run_id": "prefix_0000001_2017-09-01T12:00:00",
      "execution_date": "2017-09-19 12:00:00",
     "start_date": "2017-09-20 18:10:02.190813"
    }
  }
}
```

**Failure Response**

```
{
  "response": {
    "error": "Dag run id not found"
  }
}
```
