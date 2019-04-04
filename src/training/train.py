#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
This file contains training code for pypi insights.

Copyright © 2018 Red Hat Inc

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from rudra.data_store.aws import AmazonS3
from rudra.utils.helper import load_hyper_params
from rudra.utils.validation import BQValidation
from src.config.path_constants import (PACKAGE_TO_ID_MAP,
    MANIFEST_TO_ID_MAP, MANIFEST_PATH, HPF_MODEL_PATH, ECOSYSTEM,
    HYPERPARAMETERS_PATH, DEPLOYMENT_PREFIX, MODEL_VERSION)
from src.config.cloud_constants import (S3_BUCKET_NAME,
    AWS_S3_SECRET_KEY_ID, AWS_S3_ACCESS_KEY_ID, GITHUB_TOKEN)
from fractions import Fraction
import pandas as pd
import numpy as np
import hpfrec
import json
import daiquiri
import logging
import subprocess

daiquiri.setup(level=logging.INFO)
_logger = daiquiri.getLogger(__name__)

bq_validator = BQValidation()


def load_s3():
    """Create connection s3."""
    s3_object = AmazonS3(bucket_name=S3_BUCKET_NAME,
                         aws_access_key_id=AWS_S3_ACCESS_KEY_ID,
                         aws_secret_access_key=AWS_S3_SECRET_KEY_ID)

    s3_object.connect()
    if s3_object.is_connected():
        _logger.info("S3 connection established.")
        return s3_object

    raise Exception("S3 Connection Failed")


def load_data(s3_client):
    """Load data from s3 bucket."""
    if ((s3_client.object_exists(PACKAGE_TO_ID_MAP)) and
            (s3_client.object_exists(MANIFEST_TO_ID_MAP))):
        package_id_dict_ = s3_client.read_json_file(PACKAGE_TO_ID_MAP)
        manifest_id_dict_ = s3_client.read_pickle_file(MANIFEST_TO_ID_MAP)
        return [package_id_dict_, manifest_id_dict_]
    else:
        raw_data_dict = s3_client.read_json_file(MANIFEST_PATH)
        _logger.info("Size of Raw Manifest file is: {}".format(len(raw_data_dict)))
        return [raw_data_dict]


def make_user_item_df(manifest_dict, package_dict):
    """Make user item dataframe."""
    user_item_list = []
    for k, v in manifest_dict.items():
        user_id = int(k)
        for package in v:
            item_id = package_dict[package]
            user_item_list.append(
                {
                    "UserId": user_id,
                    "ItemId": item_id,
                    "Count": 1
                }
            )
    return user_item_list


def generate_package_id_dict(manifest_list):
    """Generate package id dictionary."""
    package_id_dict = dict()
    count = 0
    for manifest in manifest_list:
        for package_name in manifest:
            if package_name not in package_id_dict:
                package_id_dict[package_name] = count
                count += 1
    return package_id_dict


def format_dict(package_id_dict, manifest_id_dict):
    """Format the dictionaries."""
    format_pkg_id_dict = {'ecosystem': ECOSYSTEM,
                          'package_list': package_id_dict
                          }
    format_mnf_id_dict = {'ecosystem': ECOSYSTEM,
                          'manifest_list': manifest_id_dict
                          }
    return format_pkg_id_dict, format_mnf_id_dict


def generate_manifest_id_dict(manifest_list, package_id_dict):
    """Generate manifest id dictionary."""
    count = 0
    manifest_id_dict = dict()
    for manifest in manifest_list:
        package_set = set()
        for each_package in manifest:
            package_set.add(package_id_dict[each_package])
        manifest_id_dict[count] = list(package_set)
        count += 1
    return manifest_id_dict


def run_recommender(train_df, latent_factor):
    """Start the recommender."""
    recommender = hpfrec.HPF(k=latent_factor, random_seed=123,
                             ncores=-1)
    recommender.step_size = None
    _logger.warning("Model is training, Don't interrupt.")
    recommender.fit(train_df)
    return recommender


def validate_manifest_data(manifest_list):
    """Validates manifest packages with pypi."""
    for idx, manifest in enumerate(manifest_list):
        filtered_manifest = bq_validator.validate_pypi(manifest)
        # Even the filtered manifest is of length 0, we don't care about that here.
        manifest_list[idx] = filtered_manifest


def preprocess_raw_data(raw_data_dict, lower_limit, upper_limit):
    """Preprocess raw data."""
    all_manifest_list = raw_data_dict.get('package_list', [])
    _logger.info("Number of manifests collected = {}".format(
        len(all_manifest_list)))
    validate_manifest_data(all_manifest_list)
    _logger.info("Manifest list now contains only packages from pypi")
    trimmed_manifest_list = [
        manifest for manifest in all_manifest_list if lower_limit < len(manifest) < upper_limit]
    _logger.info("Number of trimmed manifest = {}".format(
        len(trimmed_manifest_list)))
    package_id_dict = generate_package_id_dict(trimmed_manifest_list)
    manifest_id_dict = generate_manifest_id_dict(trimmed_manifest_list, package_id_dict)
    return package_id_dict, manifest_id_dict


# Calculating DataFrame according to fraction
def extra_df(frac, data_df, train_df):
    """Calculate extra dataframe."""
    remain_frac = float("%.2f" % (0.80 - frac))
    len_df = len(data_df.index)
    no_rows = round(remain_frac * len_df)
    df_remain = pd.concat([data_df, train_df]).drop_duplicates(keep=False)
    df_remain_rand = df_remain.sample(frac=1)
    return df_remain_rand[:no_rows]


def train_test_split(data_df):
    """Split for training and testing."""
    data_df = data_df.sample(frac=1)
    df_user = data_df.drop_duplicates(['UserId'])
    data_df = data_df.sample(frac=1)
    df_item = data_df.drop_duplicates(['ItemId'])
    train_df = pd.concat([df_user, df_item]).drop_duplicates()
    fraction = round(Fraction(data_df, train_df), 2)

    if fraction < 0.80:
        df_ = extra_df(fraction, data_df, train_df)
        train_df = pd.concat([train_df, df_])
    test_df = pd.concat([data_df, train_df]).drop_duplicates(keep=False)
    _logger.info("Size of Training DF {} and Testing DF are: {}".format(
        len(train_df), len(test_df)))
    return train_df, test_df


def preprocess_data(data_list, lower_limit, upper_limit):
    """Preprocess data."""
    if len(data_list) == 2:
        package_dict = (data_list[0]).get('package_list')
        manifest_dict = (data_list[1]).get('manifest_list')
        _logger.info("Size of Package ID dictionary {} and Manifest ID dictionary are: {}".format(
            len(package_dict), len(manifest_dict)))
        return package_dict, manifest_dict
    else:
        raw_data = data_list[0]
        package_dict, manifest_dict = preprocess_raw_data(raw_data, lower_limit, upper_limit)
        _logger.info("Size of Package ID dictionary {} and Manifest ID dictionary are: {}".format(
            len(package_dict), len(manifest_dict)))
        return package_dict, manifest_dict


# Calculating recall according to no of recommendations
def recall_at_m(m, test_df, recommender, user_count):
    """Calculate recall at `m`."""
    recall = []
    for i in range(user_count):
        x = np.array(test_df.loc[test_df.UserId.isin([i])].ItemId)
        rec_l = x.size
        recommendations = recommender.topN(user=i, n=m, exclude_seen=True)
        intersection_length = np.intersect1d(x, recommendations).size
        try:
            recall.append({"recall": intersection_length / rec_l, "length": rec_l, "user": i})
        except ZeroDivisionError:
            pass
    return np.mean(recall)


def precision_at_m(m, test_df, recommender, user_count):
    """Calculate precision at `m`."""
    precision = []
    for i in range(user_count):
        x = np.array(test_df.loc[test_df.UserId.isin([i])].ItemId)
        recommendations = recommender.topN(user=i, n=m, exclude_seen=True)
        r_size = recommendations.size
        intersection_length = np.intersect1d(x, recommendations).size
        try:
            precision.append({"precision": intersection_length / r_size, "length": r_size,
                              "user": i})
        except ZeroDivisionError:
            pass
    return np.mean(precision)


def precision_recall_at_m(m, test_df, recommender, user_item_df):
    """Precision and recall at given `m`."""
    user_count = user_item_df.groupby("UserId").size
    try:
        precision = precision_at_m(m, test_df, recommender, user_count)
        recall = recall_at_m(m, test_df, recommender, user_count)
        _logger.info("Precision {} and Recall are: {}".format(
            precision, recall))
        return precision, recall
    except ValueError:
        pass


def save_model(s3_client, recommender):
    """Save model on s3."""
    try:
        status = s3_client.write_pickle_file(HPF_MODEL_PATH, recommender)
        _logger.info("Model has been saved {}.".format(status))
    except Exception as exc:
        _logger.error(str(exc))


def save_dictionaries(s3_client, package_id_dict, manifest_id_dict):
    """Save the dictionaries for scoring."""
    if not ((s3_client.object_exists(PACKAGE_TO_ID_MAP)) and
            (s3_client.object_exists(MANIFEST_TO_ID_MAP))):
        pkg_status = s3_client.write_json_file(PACKAGE_TO_ID_MAP,
                                               package_id_dict)
        mnf_status = s3_client.write_json_file(MANIFEST_TO_ID_MAP,
                                               manifest_id_dict)

        if not pkg_status or mnf_status:
            raise Exception("Unable to store data files for scoring")

        _logger.info("Saved dictionaries successfully")


def save_hyperparams(s3_client, content_json):
    """Save hyperparameters."""
    status = s3_client.write_json_file(HYPERPARAMETERS_PATH, content_json)
    if not status:
        raise Exception("Unable to store hyperparameters file")
    _logger.info("Hyperparameters saved")


def save_obj(s3_client, trained_recommender, precision_30, recall_30,
             package_id_dict, manifest_id_dict, precision_50, recall_50,
             lower_lim, upper_lim, latent_factor):
    """Save the objects in s3 bucket."""
    _logger.info("Trying to save the model.")
    save_model(s3_client, trained_recommender)
    save_dictionaries(s3_client, package_id_dict, manifest_id_dict)
    contents = {
        "minimum_length_of_manifest": lower_lim,
        "maximum_length_of_manifest": upper_lim,
        "precision_at_30": precision_30,
        "recall_at_30": recall_30,
        "precision_at_50": precision_50,
        "recall_at_50": recall_50,
        "latent_factor": latent_factor
    }
    save_hyperparams(s3_client, contents)


def create_git_pr(s3_client, model_version, recall_at_30):
    """Create a git PR automatically if recall_at_30 is higher than previous iteration."""
    keys = [i.key for i in s3_client.list_bucket_objects(prefix=ECOSYSTEM + DEPLOYMENT_PREFIX)]
    dates = []
    for i in keys:
        if "intermediate-model/hyperparameters.json" in i:
            dates.append(i.split('/')[2])
    dates.remove(model_version)
    previous_version = max(dates)
    k = 'maven/{depl_prefix}/{prev_ver}/intermediate-model/hyperparameters.json'.format(
        depl_prefix=DEPLOYMENT_PREFIX, prev_ver=previous_version
    )
    prev_hyperparams = s3_client.read_json_file(k)

    # Convert the json description to string
    description = json.dumps(prev_hyperparams).replace('"', '\\"')

    prev_recall = prev_hyperparams.get('recall_at_30', 0.55)
    if recall_at_30 >= prev_recall:
        try:
            # Invoke bash script to create a saas-analytics PR
            t = subprocess.Popen(['sh', 'rudra/utils/github_helper.sh', 'f8a-hpf-insights.yaml',
                                  'MODEL_VERSION', str(model_version), description],
                                 shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Wait for the subprocess to get over
            t.wait(60)
            if t.returncode == 0:
                _logger.info("Successfully created a PR")
        except ValueError:
            _logger.error('ERROR - Wrong number of arguments passed to subprocess')
            raise ValueError
        except subprocess.TimeoutExpired as s:
            t.kill()
            _logger.error("ERROR - Script Timeout during PR creation")
            raise s
        except subprocess.SubprocessError as e:
            _logger.error('ERROR - Some unknown error happened')
            _logger.error('%r' % e)
            raise e


def train_model():
    """Training model."""
    s3_obj = load_s3()
    data = load_data(s3_obj)
    hyper_params = load_hyper_params() or {}
    lower_limit = int(hyper_params.get('lower_limit', 13))
    upper_limit = int(hyper_params.get('upper_limit', 15))
    latent_factor = int(hyper_params.get('latent_factor', 300))
    _logger.info("Lower limit {}, Upper limit {} and latent factor {} are used."
                 .format(lower_limit, upper_limit, latent_factor))
    package_id_dict, manifest_id_dict = preprocess_data(data, lower_limit, upper_limit)
    user_item_list = make_user_item_df(manifest_id_dict, package_id_dict)
    user_item_df = pd.DataFrame(user_item_list)
    training_df, testing_df = train_test_split(user_item_df)
    format_pkg_id_dict, format_mnf_id_dict = format_dict(package_id_dict, manifest_id_dict)
    trained_recommender = run_recommender(training_df, latent_factor)
    precision_at_30, recall_at_30 = precision_recall_at_m(30, testing_df, trained_recommender,
                                                          user_item_df)
    precision_at_50, recall_at_50 = precision_recall_at_m(50, testing_df, trained_recommender,
                                                          user_item_df)
    try:
        save_obj(s3_obj, trained_recommender, precision_at_30, recall_at_30,
                 format_pkg_id_dict, format_mnf_id_dict, precision_at_50, recall_at_50,
                 lower_limit, upper_limit, latent_factor)
        if GITHUB_TOKEN:
            create_git_pr(s3_client=s3_obj, model_version=MODEL_VERSION, recall_at_30=recall_at_30)
    except Exception as error:
        _logger.error(error)
        raise


if __name__ == '__main__':
    train_model()
