# Copyright 2017 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import re
import numpy as np

import jieba

from pyspark.ml import Estimator, Transformer, Pipeline
from pyspark.ml.feature import HashingTF, Word2Vec, Param, Params, TypeConverters
from pyspark.sql.functions import udf
from pyspark.sql import functions as f
from pyspark.sql.types import *
from pyspark.sql.functions import lit

from sparkdl.nlp.text_analysis import TextAnalysis
from sparkdl.param.shared_params import HasEmbeddingSize, HasSequenceLength
from sparkdl.param import (
    keyword_only, HasInputCol, HasOutputCol)
import sparkdl.utils.jvmapi as JVMAPI


class SKLearnTextTransformer(Transformer, Estimator, HasInputCol, HasOutputCol):
    @keyword_only
    def __init__(self, inputCol=None, outputCol=None):
        super(SKLearnTextTransformer, self).__init__()
        kwargs = self._input_kwargs
        self.setParams(**kwargs)

    @keyword_only
    def setParams(self, inputCol=None, outputCol=None):
        kwargs = self._input_kwargs
        return self._set(**kwargs)

    def _transform(self, dataset):
        ds = dataset.withColumn("words", f.split(dataset[self.getInputCol()], "\\s+"))
        hashingTF = HashingTF(inputCol="words", outputCol=self.getOutputCol())
        new_ds = hashingTF.transform(ds)
        new_ds.show(truncate=False)
        return new_ds

    def _fit(self, dataset):
        pass


class TFTextTransformer(Transformer, Estimator, HasInputCol, HasOutputCol, HasEmbeddingSize, HasSequenceLength):
    """
    Convert sentence/document to a 2-D Array eg. [[word embedding],[....]]  in DataFrame which can be processed
    directly by tensorflow or keras who's backend is tensorflow.

    Processing Steps:

    * Using Word2Vec compute Map(word -> vector) from input column, then broadcast the map.
    * Process input column (which is text),split it with white space, replace word with vector, padding the result to
      the same size.
    * Create a new dataframe with columns like new 2-D array , vocab_size, embedding_size
    * return then new dataframe
    """

    def _fit(self, dataset):
        pass

    VOCAB_SIZE = 'vocab_size'
    EMBEDDING_SIZE = 'embedding_size'

    textAnalysisParams = Param(Params._dummy(), "textAnalysisParams", "text analysis params",
                               typeConverter=TypeConverters.identity)

    def setTextAnalysisParams(self, value):
        return self._set(textAnalysisParams=value)

    def getTextAnalysisParams(self):
        return self.getOrDefault(self.textAnalysisParams)

    shape = Param(Params._dummy(), "shape", "result shape",
                  typeConverter=TypeConverters.identity)

    def setShape(self, value):
        return self._set(shape=value)

    def getShape(self):
        return self.getOrDefault(self.shape)

    @keyword_only
    def __init__(self, inputCol=None, outputCol=None, textAnalysisParams={}, shape=(64, 100), embeddingSize=100,
                 sequenceLength=64):
        super(TFTextTransformer, self).__init__()
        kwargs = self._input_kwargs
        self._setDefault(textAnalysisParams={})
        self._setDefault(embeddingSize=100)
        self._setDefault(sequenceLength=64)
        self._setDefault(shape=(self.getSequenceLength(), self.getEmbeddingSize()))
        self.setParams(**kwargs)

    @keyword_only
    def setParams(self, inputCol=None, outputCol=None, textAnalysisParams={}, shape=(64, 100), embeddingSize=100,
                  sequenceLength=64):
        kwargs = self._input_kwargs
        return self._set(**kwargs)

    def _transform(self, dataset):
        sc = JVMAPI._curr_sc()

        word2vec = Word2Vec(vectorSize=self.getEmbeddingSize(), minCount=1, inputCol=self.getInputCol(),
                            outputCol="word_embedding")

        archiveAutoExtract = sc._conf.get("spark.master").lower().startswith("yarn")
        zipfiles = []
        if not archiveAutoExtract and "dicZipName" in self.getTextAnalysisParams():
            dicZipName = self.getTextAnalysisParams()["dicZipName"]
            if "spark.files" in sc._conf:
                zipfiles = [f.split("/")[-1] for f in sc._conf.get("spark.files").split(",") if
                            f.endswith("{}.zip".format(dicZipName))]

        dicDir = self.getTextAnalysisParams()["dicDir"] if "dicDir" in self.getTextAnalysisParams() else ""

        def lcut(s):
            TextAnalysis.load_dic(dicDir, archiveAutoExtract, zipfiles)
            return jieba.lcut(s)

        lcut_udf = udf(lcut, ArrayType(StringType()))
        vectorsDf = word2vec.fit(
            dataset.select(lcut_udf(self.getInputCol()).alias(self.getInputCol()))).getVectors()

        """
          It's strange here that after calling getVectors the df._sc._jsc will lose and this is
          only happens when you run it with ./python/run-tests.sh script.
          We add this code to make it pass the test. However it seems this will hit
          "org.apache.spark.SparkException: EOF reached before Python server acknowledged" error.
        """
        if vectorsDf._sc._jsc is None:
            vectorsDf._sc._jsc = sc._jsc

        word_embedding = dict(vectorsDf.rdd.map(
            lambda p: (p.word, p.vector.values.tolist())).collect())

        word_embedding["unk"] = np.zeros(self.getEmbeddingSize()).tolist()
        local_word_embedding = sc.broadcast(word_embedding)

        not_array_2d = len(self.getShape()) != 2 or self.getShape()[0] != self.getSequenceLength()

        def convert_word_to_index(s):
            def _pad_sequences(sequences, maxlen=None):
                new_sequences = []

                if len(sequences) <= maxlen:
                    for i in range(maxlen - len(sequences)):
                        new_sequences.append(np.zeros(self.getEmbeddingSize()).tolist())
                    return sequences + new_sequences
                else:
                    return sequences[0:maxlen]

            new_q = [local_word_embedding.value[word] for word in re.split(r"\s+", s) if
                     word in local_word_embedding.value.keys()]
            result = _pad_sequences(new_q, maxlen=self.getSequenceLength())
            if not_array_2d:
                result = np.array(result).reshape(self.getShape()).tolist()
            return result

        cwti_udf = udf(convert_word_to_index, ArrayType(ArrayType(FloatType())))

        if not_array_2d:
            cwti_udf = udf(convert_word_to_index, ArrayType(FloatType()))

        doc_martic = (dataset.withColumn(self.getOutputCol(), cwti_udf(self.getInputCol()).alias(self.getOutputCol()))
                      .withColumn(self.VOCAB_SIZE, lit(len(word_embedding)))
                      .withColumn(self.EMBEDDING_SIZE, lit(self.getEmbeddingSize()))
                      )

        return doc_martic
