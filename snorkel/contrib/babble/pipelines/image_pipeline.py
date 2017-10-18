import csv
from itertools import product
import os
import random
import re

import numpy as np
import pandas as pd
from pprint import pprint

from snorkel.parser import ImageCorpusExtractor, CocoPreprocessor
from snorkel.models import StableLabel
from snorkel.db_helpers import reload_annotator_labels
from snorkel.annotations import load_marginals, load_gold_labels

from snorkel.contrib.babble import Babbler
from snorkel.contrib.babble.pipelines import BabblePipeline, final_report

from tutorials.babble import MTurkHelper

class ImagePipeline(BabblePipeline):

    def extract(self):
        print("Extraction was performed during parse stage.")
        for split in self.config['splits']:
            num_candidates = self.session.query(self.candidate_class).filter(
                self.candidate_class.split == split).count()
            print("Candidates [Split {}]: {}".format(split, num_candidates))


    def classify(self, config=None, slim_ws_path=None):
        if config:
            self.config = config
        if not slim_ws_path:
            slim_ws_path = self.config['slim_ws_path']

        def get_candidates(self, split):
            return self.session.query(self.candidate_class).filter(
                self.candidate_class.split == split)

        def create_csv(dataset_dir, filename, coco_ids, labels, setname=None):
            csv_name = os.path.join(dataset_dir, filename)
            with open(csv_name, 'w') as csvfile:
                csvwriter = csv.writer(csvfile)
                num_images = 0

                for idx in range(len(coco_ids)):
                    if coco_ids[idx] == 0:
                        continue
                    else:
                        num_images += 1
                        url = 'http://images.cocodataset.org/{}/{:012}.jpg'.format(setname,int(coco_ids[idx]))
                        csvwriter.writerow([url,labels[idx]])
            return num_images

        def link_images_candidates(anns, candidates, mscoco, marginals):
            """
            Stores a max-pooled label per image based on bbox-level annotations.
            :param anns: np.array (of what?)
            :param candidates: list of candidates.
            :param mscoco: np.array (of what?)
            :param marginals: np.array of marginal probababilities per candidate.
            """
            coco_ids =  np.zeros(len(anns))
            labels = np.zeros(len(anns))
            num_candidates = len(candidates)

            for idx in range(num_candidates):
                cand = candidates[idx]
                image_id = int(cand[1].stable_id.split(":")[1])
                mscoco_id = mscoco[image_id]

                coco_ids[image_id] = int(mscoco_id)
                try:
                    labels[image_id] = max(labels[image_id], max(marginals[idx], 0))
                except:
                    import pdb; pdb.set_trace()

            return coco_ids, labels

        def print_settings(settings):
            for k, v in sorted(settings.items()):
                print("{}: {}".format(k, v))

        def scrape_output(output_file):
            with open(output_file, mode='rb') as output:
                value_rgx = r'eval/\w+\[([\d\.]+)\]'
                for row in output:
                    if 'eval/Accuracy' in row:
                        accuracy = float(re.search(value_rgx, row).group(1))
                    elif 'eval/Precision' in row:
                        precision = float(re.search(value_rgx, row).group(1))
                    elif 'eval/Recall' in row:
                        recall = float(re.search(value_rgx, row).group(1))
                    else:
                        continue
                return accuracy, precision, recall

        if self.config['seed']:
            np.random.seed(self.config['seed'])

        dataset_dir = os.path.join(slim_ws_path, 'datasets/mscoco/', self.config['domain'])
        if not os.path.exists(dataset_dir):
            os.makedirs(dataset_dir)

        X_train = self.get_candidates(0)
        Y_train = (self.train_marginals if getattr(self, 'train_marginals', None) 
                   is not None else load_marginals(self.session, split=0))
        Y_train_gold = np.array(load_gold_labels(self.session, annotator_name='gold', split=0).todense()).ravel()
        X_val = self.get_candidates(1)
        Y_val = np.array(load_gold_labels(self.session, annotator_name='gold', split=1).todense()).ravel()

        # Save out Validation Images and Labels
        if not getattr(self, 'anns_path', False):
            self.anns_path = self.config['anns_path']
        val_anns = np.load(self.anns_path + self.config['domain'] + '_val_anns.npy').tolist()
        val_mscoco = np.load(self.anns_path + self.config['domain'] + '_val_mscoco.npy')
        val_coco_ids, val_labels = link_images_candidates(val_anns, X_val, val_mscoco, Y_val)

        # Split validation set 50/50 into val/test
        num_labeled = len(val_labels)
        assignments = np.random.permutation(num_labeled)
        val_assignments = assignments[:num_labeled*2/5]
        test_assignments = assignments[num_labeled*2/5:]
        test_coco_ids, test_labels = val_coco_ids[test_assignments], val_labels[test_assignments]
        val_coco_ids, val_labels = val_coco_ids[val_assignments], val_labels[val_assignments]

        num_dev = create_csv(dataset_dir, 'validation_images.csv', val_coco_ids, val_labels, 'val2017')
        num_test = create_csv(dataset_dir, 'test_images.csv', test_coco_ids, test_labels, 'val2017')

        train_anns = np.load(self.anns_path + self.config['domain'] + '_train_anns.npy').tolist()
        train_mscoco = np.load(self.anns_path + self.config['domain'] + '_train_mscoco.npy')

        # If we're in traditional supervision mode, use hard marginals from the train set
        if self.config['supervision'] == 'traditional':
            train_size = self.config['max_train']
            Y_train = Y_train_gold  # use 0/1 labels, not probabilistic labels
            # This zips X and Y, sorts by image_id, keeps only train_size pairs,
            # and returns them to X and Y
            X_train, Y_train = zip(*(sorted(zip(X_train, Y_train), 
                                 key=lambda x: x[0][1].stable_id.split(":")[1])[:train_size]))

            train_coco_ids, train_labels = link_images_candidates(train_anns, X_train, train_mscoco, Y_train_gold)

        train_coco_ids, train_labels = link_images_candidates(train_anns, X_train, train_mscoco, Y_train)
        num_train = create_csv(dataset_dir, 'train_images.csv', train_coco_ids, train_labels, 'train2017')

        if self.config['verbose']:
            print("Train size: {}".format(num_train))
            print("Dev size: {}".format(num_dev))
            print("Test size: {}".format(num_test))

        # Convert to TFRecords Format
        if self.config.get('download_data', False):
            print ('Downloading and converting images...')
            os.system('python ' + os.path.join(slim_ws_path, 'download_and_convert_data.py') + \
                      ' --dataset_name mscoco ' + \
                      ' --dataset_dir ' + dataset_dir)
        else:
            print("Assuming MSCOCO data is already downloaded and converted (download_data = False).")
        
        # Call TFSlim Model
        train_root = os.path.join(dataset_dir, 'train/')
        eval_root = os.path.join(dataset_dir, 'eval/')

        # Run homemade hacky random search
        # First, make random assignments in space of possible configurations
        param_names = self.config['disc_params_search'].keys()
        param_assignments = list(product(*[self.config['disc_params_search'][pn] for pn in param_names]))
        disc_params_list = [{k: v for k, v in zip(param_names, param_assignments[i])} for i in range(len(param_assignments))]
        # Randomnly select a small number of these to try
        random.shuffle(disc_params_list)
        disc_params_options = disc_params_list[:self.config['disc_model_search_space']]

        print("Starting training over space of {} configurations".format(
            min(self.config['disc_model_search_space'], len(disc_params_options))))

        accuracies, precisions, recalls = [], [], []
        for i, disc_params in enumerate(disc_params_options):
            train_dir = os.path.join(train_root, "config_{}".format(i))
            eval_dir = os.path.join(eval_root, "config_{}".format(i))
            print("\nConfiguration {}.".format(i, eval_dir))
            print("Running the following configuration:".format(i))
            print_settings(disc_params)

            # print('Calling TFSlim train...')
            # TODO: launch these in parallel
            if not os.path.exists(train_dir):
                os.makedirs(train_dir)
            train_cmd = 'python ' + slim_ws_path + 'train_image_classifier.py ' + \
                ' --train_dir=' + train_dir + \
                ' --dataset_name=mscoco' + \
                ' --dataset_split_name=train' + \
                ' --dataset_dir=' + dataset_dir + \
                ' --model_name=' + str(self.config['disc_model_class']) + \
                ' --optimizer=' + str(self.config['optimizer']) + \
                ' --num_clones=' + str(self.config['parallelism']) + \
                ' --log_every_n_steps=' + str(self.config['print_freq']) + \
                ' --learning_rate=' + str(disc_params['lr']) + \
                ' --max_number_of_steps=' + str(disc_params['max_steps'])
            os.system(train_cmd)

            print('Calling TFSlim eval on validation...')
            output_file = os.path.join(eval_dir, 'output.txt')
            if not os.path.exists(eval_dir):
                os.makedirs(eval_dir)
            eval_cmd = 'python '+ slim_ws_path + 'eval_image_classifier.py ' + \
                  ' --dataset_name=mscoco ' + \
                  ' --dataset_dir=' + dataset_dir + \
                  ' --checkpoint_path=' + train_dir + \
                  ' --eval_dir=' + eval_dir + \
                  ' --dataset_split_name=validation ' + \
                  ' --model_name=' + str(self.config['disc_model_class']) + \
                  ' &> ' + output_file
            os.system(eval_cmd)

            # Scrape results from output.txt 
            accuracy, precision, recall = scrape_output(output_file)
            accuracies.append(accuracy)
            precisions.append(precision)
            recalls.append(recall)
        
        # Calculate F1 scores
        f1s = [float(2 * p * r)/(p + r) if p and r else 0 for p, r in zip(precisions, recalls)]
        dev_results = {
            'accuracy':     pd.Series(accuracies),
            'precision':    pd.Series(precisions),
            'recall':       pd.Series(recalls),
            'f1':           pd.Series(f1s)
        }
        dev_df = pd.DataFrame(dev_results)
        print("\nDev Results: {}")
        print(dev_df)
        best_config_idx = dev_df['f1'].idxmax()

        # Identify best configuration and run on test
        print("\nBest configuration ({}):".format(best_config_idx))
        print_settings(disc_params_options[best_config_idx])
        checkpoint_path = os.path.join(train_root, "config_{}".format(best_config_idx))
        eval_dir = os.path.join(eval_root, "config_{}".format(best_config_idx))
        test_file = os.path.join(eval_dir, 'test_output.txt')

        print('\nCalling TFSlim eval on test...')
        os.system('python '+ slim_ws_path + 'eval_image_classifier.py ' + \
                 ' --dataset_name=mscoco '
                 ' --dataset_dir=' + dataset_dir + \
                 ' --checkpoint_path=' + checkpoint_path + \
                 ' --eval_dir=' + eval_dir + \
                 ' --dataset_split_name=test ' + \
                 ' --model_name=' + str(self.config['disc_model_class']) + \
                 ' &> ' + test_file)
        
        accuracy, precision, recall = scrape_output(test_file)
        p, r = precision, recall
        f1 = float(2 * p * r)/(p + r) if p and r else 0

        test_results = {
            # 'accuracy':     pd.Series([accuracy]),
            'precision':    pd.Series([precision]),
            'recall':       pd.Series([recall]),
            'f1':           pd.Series([f1])
        }
        test_df = pd.DataFrame(test_results)
        print("\nTest Results: {}")
        print(test_df)

        if not getattr(self, scores, False):
            self.scores = {}
        self.scores['Disc'] = [precision, recall, f1]
        print("\nWriting final report to {}".format(self.config['log_dir']))
        final_report(self.config, self.scores)