import json
import re
import unittest
from StringIO import StringIO

import mock
from mock import patch
from mock import Mock

import metric_collector_handlers as handlers

import spectator_client
import spectator_client_test as sample_data


def TABLE(row_html_list):
  return '<table>{0}</table>'.format(''.join(row_html_list))

def TR(*column_lists):
  columns = []
  for column_list in column_lists:
    for column in column_list:
      columns.append(column)
  return '<tr>{0}</tr>'.format(''.join(columns))

def TH(names):
  return ['<th>{0}</th>'.format(name) for name in names]

def TD(cells, rowspan=None):
  attributes = ''
  if rowspan is not None:
    attributes += ' rowspan={0}'.format(rowspan)
  return ['<td{0}>{1}</td>'.format(attributes, cell) for cell in cells]


class MetricCollectorHandlersTest(unittest.TestCase):
  def setUp(self):
    self.mock_options = Mock()
    self.mock_options.prototype_path = None
    self.mock_options.host = 'spectator_hostname'
    self.mock_options.services = ['clouddriver', 'gate']
    self.spectator = spectator_client.SpectatorClient(self.mock_options)

    expect_clouddriver = sample_data.SAMPLE_CLOUDDRIVER_RESPONSE_OBJ
    expect_gate = sample_data.SAMPLE_GATE_RESPONSE_OBJ

    self.mock_clouddriver_response = (
      StringIO(sample_data.SAMPLE_CLOUDDRIVER_RESPONSE_TEXT))
    self.mock_gate_response = StringIO(sample_data.SAMPLE_GATE_RESPONSE_TEXT)

    self.mock_request = Mock()
    self.mock_request.respond = Mock()

  @patch('spectator_client.urllib2.urlopen')
  def test_dump_handler_default(self, mock_urlopen):
    expected_by_service = {
        'clouddriver': sample_data.SAMPLE_CLOUDDRIVER_RESPONSE_OBJ,
        'gate': sample_data.SAMPLE_GATE_RESPONSE_OBJ
    }
    mock_urlopen.side_effect = [self.mock_clouddriver_response,
                                self.mock_gate_response]

    dump = handlers.DumpMetricsHandler(self.mock_options, self.spectator)

    params = {}
    dump(self.mock_request, '/dump', params, '')
    called_with = self.mock_request.respond.call_args[0]
    self.assertEqual(200, called_with[0])
    self.assertEqual({'ContentType': 'application/json'}, called_with[1])
    doc = json.JSONDecoder(encoding='utf-8').decode(called_with[2])
    self.assertEqual(expected_by_service, doc)

  def test_explore_to_service_tag_one(self):
    klass = handlers.ExploreCustomDescriptorsHandler
    type_map = spectator_client.SpectatorClient.service_map_to_type_map(
        {'clouddriver': sample_data.SAMPLE_CLOUDDRIVER_RESPONSE_OBJ})
    service_tag_map, services = klass.to_service_tag_map(type_map)
    expect = {
        'jvm.buffer.memoryUsed': {
            'clouddriver' : [{'id': 'mapped'}, {'id': 'direct'}],
         },
        'jvm.gc.maxDataSize': {
            'clouddriver' : [{None: None}]
        },
        'tasks': {
            'clouddriver' : [{'success': 'true'}]
        }
    }
    self.assertEqual(expect, service_tag_map)
    self.assertEqual(set(['clouddriver']), services)

  def test_explore_to_service_tag_map_two(self):
    klass = handlers.ExploreCustomDescriptorsHandler
    type_map = spectator_client.SpectatorClient.service_map_to_type_map(
        {'clouddriver': sample_data.SAMPLE_CLOUDDRIVER_RESPONSE_OBJ})
    spectator_client.SpectatorClient.ingest_metrics(
        'gate', sample_data.SAMPLE_GATE_RESPONSE_OBJ, type_map)
    usage, services = klass.to_service_tag_map(type_map)
    expect = {
        'controller.invocations': {
            'gate' : [{'controller': 'PipelineController',
                       'method': 'savePipeline'}]
        },
        'jvm.buffer.memoryUsed': {
            'clouddriver' : [{'id': 'mapped'}, {'id': 'direct'}],
            'gate' : [{'id': 'mapped'}, {'id': 'direct'}],
         },
        'jvm.gc.maxDataSize': {
            'clouddriver' : [{None: None}],
            'gate' : [{None: None}],
        },
        'tasks': {
            'clouddriver' : [{'success': 'true'}]
        }
    }

    self.assertEqual(set(['clouddriver', 'gate']), services)
    self.assertEqual(expect, usage)

  def test_to_tag_service_map(self):
    klass = handlers.ExploreCustomDescriptorsHandler
    service_tag_map = {
        'A': [{'x': 'X', 'y': 'Y'}, {'x': '1', 'y': '2'}],
        'B': [{'x': 'X', 'z': 'Z'}, {'x': 'b', 'z': '3'}]}
    columns = {'A': 0, 'B': 1}
    expect = {'x': [set(['X', '1']), set(['X', 'b'])],
              'y': [set(['Y', '2']), set()],
              'z': [set(), set(['Z', '3'])]}

    inverse_map = klass.to_tag_service_map(columns, service_tag_map)
    self.assertEqual(expect, inverse_map)

  @patch('spectator_client.urllib2.urlopen')
  def test_explore_custom_descriptors_default(self, mock_urlopen):
    klass = handlers.ExploreCustomDescriptorsHandler
    explore = klass(self.mock_options, self.spectator)

    mock_urlopen.side_effect = [self.mock_clouddriver_response,
                                self.mock_gate_response]

    self.mock_request.build_html_document = lambda body, title: body
    params = {}
    explore(self.mock_request, '/explore', params, '')
    called_with = self.mock_request.respond.call_args[0]
    self.assertEqual(200, called_with[0])
    self.assertEqual({'ContentType': 'text/html'}, called_with[1])
    html = minimize_html(called_with[2])

    make_link = lambda name: '<A href="/show?meterNameRegex={0}">{0}</A>'.format(
        name)
    expect = TABLE([
        TR(TH(['Metric',
               'Label',
               'clouddriver',
               'gate'])),
        TR(TD([make_link('controller.invocations')], rowspan=2),
           TD(['controller',
               '',
               'PipelineController'])),
        TR(TD(['method',
               '',
               'savePipeline'])),
        TR(TD([make_link('jvm.buffer.memoryUsed'),
               'id',
               'direct, mapped',
               'direct, mapped'])),
        TR(TD([make_link('jvm.gc.maxDataSize'),
               '',
               'n/a',
               'n/a'])),
        TR(TD([make_link('tasks'),
               'success',
               'true',
               ''])),
    ])
    self.assertEqual(expect, html)


def minimize_html(html):
  html = html.replace('\n', '')
  return re.sub(r' border=1', '', html)

if __name__ == '__main__':
  # pylint: disable=invalid-name
  loader = unittest.TestLoader()
  suite = loader.loadTestsFromTestCase(MetricCollectorHandlersTest)
  unittest.TextTestRunner(verbosity=2).run(suite)
