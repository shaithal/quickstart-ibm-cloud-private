#!/usr/bin/python
###############################################################################
# Licensed Material - Property of IBM
# 5724-I63, 5724-H88, (C) Copyright IBM Corp. 2018 - All Rights Reserved.
# US Government Users Restricted Rights - Use, duplication or disclosure
# restricted by GSA ADP Schedule Contract with IBM Corp.
#
# DISCLAIMER:
# The following source code is sample code created by IBM Corporation.
# This sample code is provided to you solely for the purpose of assisting you
# in the  use of  the product. The code is provided 'AS IS', without warranty or
# condition of any kind. IBM shall not be liable for any damages arising out of
# your use of the sample code, even if IBM has been advised of the possibility
# of such damages.
###############################################################################

'''
Created on 30 MAY 2018

@author: Peter Van Sickel pvs@us.ibm.com

Description:
  Bootstrap script for ICP AWS Quickstart
  
  All of this is expected to run on a "boot" node that is used to do the ICP installation.
  The boot node is expected to be separate from any other cluster node.
  Ansible is used for some of the work done by the boot node:
    installing docker on each cluster node:
    configuring certain Linux parameters

History:
  30 MAY 2018 - pvs - Initial creation.
  
  19-21 JUN 2018 - pvs - Added code to introspect AWS CF stack and create hosts file.
  
  22-23 JUN 2018 - pvs - Added code to configure SSH
  
  24 JUN 2018 - pvs - Added code to put public key into AWS SSM Parameter cache.
  
  05 AUG 2018 - pvs - Added code to synchronize with cluster nodes.
  
  06-09  AUG 2018 - pvs - Added code to create the configuration file, run the inception pre-configuration and installation.
  Added code to do the docker image load locally on each cluster node.  Added the code to coordinate the image load on each
  cluster node with the boot node so the image load occurs after the boot node runs the Ansible playbook to install docker.
  Boot node waits for notification from all the cluster nodes that the image load has completed.
  
  10-11 AUG 2018 - pvs - Added code to handle the installation of the ICP 2.1.0.3 fixpack1.
  Added code to handle configuration of PKI artifacts for ICP. (If we let ICP create the PKI artifacts using the ELB
  DNS name, the SAN exceeds the 64 character limit.  In one of the test deployments that failed the ELB DNS name was
  71 characters long.
  
  For ICP KC doc on PKI configuration see: 
    https://www.ibm.com/support/knowledgecenter/SSBS6K_2.1.0.3/installing/create_ca_cert.html
    
  01 SEP 2018 - pvs - Added code to wait for the root stack to have a status of CREATE_COMPLETE.
  In testing, hit a case where the bootnode script was getting outputs from the root stack and 
  the outputs were empty.
  
  16 OCT 2018 - pvs - Added code to get binary content needed for the ICP installation using 
  pressigned S3 URL and the Python requests module.
  
  18 OCT 2018 - pvs - Added code to handle configuration of kubectl with permanent config context.
'''

from Crypto.PublicKey import RSA
from subprocess import call
import socket
import shutil
import requests
from os import chmod
import sys, os.path, time, datetime
import boto3
import docker
from botocore.exceptions import ClientError
from yapl.utilities.Trace import Trace, Level
import yapl.utilities.Utilities as Utilities
from yapl.exceptions.Exceptions import ExitException
from yapl.exceptions.Exceptions import MissingArgumentException
from yapl.exceptions.Exceptions import InvalidArgumentException
from yapl.exceptions.AWSExceptions import AWSStackResourceException
from yapl.exceptions.ICPExceptions import ICPInstallationException
from yapl.exceptions.Exceptions import NotImplementedException

# Having trouble getting InstallDocker to work due to Ansible Python library issues.
#from yapl.docker.InstallDocker import InstallDocker

from yapl.icp.AWSConfigureICP import ConfigureICP
from yapl.icp.AWSConfigureEFS import ConfigureEFS
from yapl.icp.ConfigurePKI import ConfigurePKI
from yapl.k8s.ConfigureKubectl import ConfigureKubectl
from yapl.docker.PrivateRegistry import PrivateRegistry

ClusterHostSyncSleepTime = 60
ClusterHostSyncMaxTryCount = 100
StackStatusMaxWaitCount = 100
StackStatusSleepTime = 60

"""
  StackParameters and StackParameterNames holds all of the CloudFormation stack parameters.  
  The __getattr__ method on the Bootstrap class is used to make StackParameters accessible
  as instance variables.
  
  Installation Parameters (in alphabetical order):
    FixpackInceptionImageName     - The name of the ICP fixpack inception image,
                                    e.g., ibmcom/icp-inception:2.1.0.3-ee-fp1
                                    
    FixpackInstallCommandString   - The command string to use to launch the ICP fixpack installation.
                                    e.g., install -v
                                    
    ICPArchivePath                - The last element of the ICPArchivePath is the ICPArchiveName
                                    e.g., ibm-cloud-private-x86_64-2.1.0.3.tar.gz
     
    ICPDeploymentLogsBucketName   - The name of the S3 bucket where the installation logs get exported.                  
    
    
    InceptionImageName            - The name of the ICP inception image, 
                                    e.g., ibmcom/icp-inception:2.1.0.3-ee
    
    InceptionInstallCommandString - The command string to use to launch the ICP installation.
                                    e.g., install -v
                                    When installing the ICP 2.1.0.3 fixpack 1 the install command
                                    string is: ./cluster/ibm-cloud-private-2.1.0.3-fp1.sh

    InstallICPFixpack             - Switch that controls whether or not to install the ICP fixpack.
    
    TBD - more that need to be documented.                        
"""

ICPClusterRoles = [ 'master', 'worker', 'proxy', 'management', 'va', 'etcd' ]
HelpFile = "bootstrap.txt"

"""
  The StackParameters are imported from the CloudFormation stack in the _init() 
  method below.
"""
StackParameters = {}
StackParameterNames = []

"""
  The SSMParameterKeys gets initialized in the _init() method. 
"""
SSMParameterKeys = []

TR = Trace(__name__)

class EFSVolume:
  """
    Simple class to manage an EFS volume.
  """
  
  def __init__(self,efsServer,mountPoint):
    """
      Constructor
    """
    self.efsServer = efsServer
    self.mountPoint = mountPoint
  #endDef
  
#endClass


class Host:
  """
    Simple class to hold information about an ICP host needed to create the hosts file
    used in the ICP installation.
    
    The instanceId is the EC2 instance ID that can be used to work with the instance
    using the boto3 Python library.
    
    Helper class to the Bootstrap class.
  """
  
  def __init__(self, ip4_address, private_dns_name, role, instanceId):
    """
      Constructor
    """
    self.ip4_address = ip4_address
    self.private_dns_name = private_dns_name
    self.role = role
    self.instanceId = instanceId
  #endDef

#endClass


class Bootstrap(object):
  """
    Bootstrap class for AWS ICP Quickstart responsible for steps executed on 
    the boot node that lead to the installation of IBM Cloud Private on a collection 
    of AMIs deployed in an AWS region.
  """

  ArgsSignature = {
                    '--help': 'string',
                    '--region': 'string',
                    '--stack-name': 'string',
                    '--root-stackid': 'string',
                    '--stackid': 'string',
                    '--role': 'string',
                    '--logfile': 'string',
                    '--loglevel': 'string',
                    '--trace': 'string'
                   }


  def __init__(self):
    """
      Constructor
      
      NOTE: Some instance variable initialization happens in self._init() which is 
      invoked early in main() at some point after getStackParameters().
      
    """
    object.__init__(self)
    
    self.rc = 0
    self.home = os.path.expanduser("~")
    self.logsHome = os.path.join(self.home,"logs")
    self.sshHome = os.path.join(self.home,".ssh")
    self.fqdn = socket.getfqdn()
    self.sshKey = None
    self.cfnClient = boto3.client('cloudformation')
    self.cfnResource = boto3.resource('cloudformation')
    self.ec2 = boto3.resource('ec2')
    self.asg = boto3.client('autoscaling')
    self.s3  = boto3.client('s3')
    self.ssm = boto3.client('ssm')
    self.route53 = boto3.client('route53')
    
    self.hosts = { 'master': [], 'worker': [], 'proxy': [], 'management': [], 'va': [], 'etcd': []}
    self.clusterHosts = []
    self.bootHost = None
    self.rootStackOutputs = {}
        
    # Where the CloudFormation template puts the inception fixpack archive.
    self.inceptionFixpackArchivePath = "/tmp/icp-inception-fixpack.tar"
    
    # Various log file paths
    self.imageArchivePath = "/tmp/icp-install-archive.tgz"
    self.icpInstallLogFilePath = os.path.join("%s" % self.logsHome, "icp-install-%s.log" % self._getTimeStamp())
    
    # Some private registry parameters (Install from private registry is not implemented yet.)
    self.serverPKIDirectory = "/opt/registry/certs"
    self.clientPKIDirectory = "/etc/docker/certs.d/%s\:8500/" % self.fqdn
    
  #endDef

  def __getattr__(self,attributeName):
    """
      Support for attributes that are defined in the StackParameterNames list
      and with values in the StackParameters dictionary.  
    """
    attributeValue = None
    if (attributeName in StackParameterNames):
      attributeValue = StackParameters.get(attributeName)
    else:
      raise AttributeError("%s is not a StackParameterName" % attributeName)
    #endIf
  
    return attributeValue
  #endDef


  def __setattr__(self,attributeName,attributeValue):
    """
      Support for attributes that are defined in the StackParameterNames list
      and with values in the StackParameters dictionary.
      
      NOTE: The StackParameters are intended to be read-only.  It's not 
      likely they would be set in the Bootstrap instance once they are 
      initialized in getStackParameters().
    """
    if (attributeName in StackParameterNames):
      StackParameters[attributeName] = attributeValue
    else:
      object.__setattr__(self, attributeName, attributeValue)
    #endIf
  #endDef

  
  def _getArg(self,synonyms,args,default=None):
    """
      Return the value from the args dictionary that may be specified with any of the
      argument names in the list of synonyms.

      The synonyms argument may be a Jython list of strings or it may be a string representation
      of a list of names with a comma or space separating each name.

      The args is a dictionary with the keyword value pairs that are the arguments
      that may have one of the names in the synonyms list.

      If the args dictionary does not include the option that may be named by any
      of the given synonyms then the given default value is returned.

      NOTE: This method has to be careful to make explicit checks for value being None
      rather than something that is just logically false.  If value gets assigned 0 from
      the get on the args (command line args) dictionary, that appears as false in a
      condition expression.  However 0 may be a legitimate value for an input parameter
      in the args dictionary.  We need to break out of the loop that is checking synonyms
      as well as avoid assigning the default value if 0 is the value provided in the
      args dictionary.
    """
    value = None
    if (type(synonyms) != type([])):
      synonyms = Utilities.splitString(synonyms)
    #endIf

    for name in synonyms:
      value = args.get(name)
      if (value != None):
        break
      #endIf
    #endFor

    if (value == None and default != None):
      value = default
    #endIf

    return value
  #endDef


  def _getTimeStamp(self):
    now = datetime.datetime.now()
    return now.strftime("%y-%m%d-%H%M")
  #endDef


  def _usage(self):
    """
      Emit usage info to stdout.
      The _usage() method is invoked by the --help option.
    """
    methodName = '_usage'
    if (os.path.exists(HelpFile)):
      Utilities.showFile(HelpFile)
    else:
      TR.info(methodName,"There is no usage information for '%s'" % __name__)
    #endIf
  #endDef


  def _configureTraceAndLogging(self,traceArgs):
    """
      Return a tuple with the trace spec and logFile if trace is set based on given traceArgs.

      traceArgs is a dictionary with the trace configuration specified.
         loglevel|trace <tracespec>
         logfile|logFile <pathname>

      If trace is specified in the trace arguments then set up the trace.
      If a log file is specified, then set up the log file as well.
      If trace is specified and no log file is specified, then the log file is
      set to "trace.log" in the current working directory.
    """
    logFile = self._getArg(['logFile','logfile'], traceArgs)
    if (logFile):
      TR.appendTraceLog(logFile)
    #endIf

    trace = self._getArg(['trace', 'loglevel'],traceArgs)

    if (trace):
      if (not logFile):
        TR.appendTraceLog('trace.log')
      #endDef

      TR.configureTrace(trace)
    #endIf
    return (trace,logFile)
  #endDef
  
  
  def _getSSMParameterKeys(self,stackName):
    """
      Fill the global SSMParameterKeys list with all the keys that will be used to
      coordinate the deployment.
      
      The _getSSMParameterKeys() method is assumed to be run after the _getHosts()
      method to fill hosts list for the instance.
    """
    methodName = "_getSSMParameterKeys"
    
    global SSMParameterKeys
    
    clusterHosts = self.getClusterHosts()
    
    # Add all the host related parameter keys
    for host in clusterHosts:
      parmKey = "/%s/%s" % (stackName,host.private_dns_name)
      if (TR.isLoggable(Level.FINEST)):
        TR.finest(methodName,"Initializing SSMParameterKeys, adding %s" % parmKey)
      #endIf
      SSMParameterKeys.append(parmKey)
    #endFor
    
    # Add the bootstrap related parameter keys
    SSMParameterKeys.append("/%s/boot-public-key" % stackName)
    SSMParameterKeys.append("/%s/docker-installation" % self.rootStackName)
    
  #endDef
  
  
  def getStackParameters(self, stackId):
    """
      Return a dictionary with stack parameter name-value pairs from the  
      CloudFormation stack with the given stackId.
    """
    result = {}
    
    stack = self.cfnResource.Stack(stackId)
    stackParameters = stack.parameters
    for parm in stackParameters:
      parmName = parm['ParameterKey']
      parmValue = parm['ParameterValue']
      result[parmName] = parmValue
    #endFor
    
    return result
  #endDef
  
  def getClusterName(self):
    """
      Return the FQDN used to access the ICP management console for this cluster. 
      
      The cluster name is the same name as the cluster CN.
      The cluster CN is the name used in the PKI certificate created for the cluster. 
      
      This is a convenience method for getting the cluster FQDN.
    """
    return self.CN
  #endDef
  
  
  def getClusterCN(self):
    """
      Return the cluster CN comprised of the ClusterName and VPCDomain. 
      A common name is needed for the ICP X.509 certificate for the cluster (e.g., the management console.
    """

    CN = "%s.%s" % (self.ClusterName,self.VPCDomain)
    return CN
  #endDef
  
  
  def _init(self, rootStackId, rootStackName, bootStackId):
    """
      Additional initialization of the Bootstrap instance based on stack parameters.
      
      The rootStackId is passed along in position 0 of the list of stack IDs that 
      is set to the stackIds instance variable.
      
      The rootStackName is used as part of the SSM parameter key.  All of the cluster 
      nodes expect to see parameters based on the root stack name.
      
      Invoke getStackParameters() gets all the CloudFormation stack parameters imported
      into the StackParmaters dictionary to make them available for use with the Bootstrap 
      instance as instance variables via __getattr__().
      
      We default the Docker client timeout to 7200 seconds to avoid a timeout during the 
      inception installation.  The timeout is configurable with the InceptionTimeout 
      parameter on the root stack template.
      
    """
    methodName = "_init"
    global StackParameters, StackParameterNames
    
    bootStackParameters = self.getStackParameters(bootStackId)
    rootStackParameters = self.getStackParameters(rootStackId)
    StackParameters = self.addParameters(bootStackParameters,rootStackParameters)
    StackParameterNames = StackParameters.keys()
    
    if (TR.isLoggable(Level.FINEST)):
      TR.finest(methodName,"StackParameterNames: %s" % StackParameterNames)
    #endIf
      
    # Create a second icpVersion that is the same as the ICPVersion from the stack parameters
    # but with the dots removed.  ICPVersion must be provided as a stack parameter.
    self.icpVersion = self.ICPVersion.replace('.','')
    self.icpHome = "/opt/icp/%s" % self.ICPVersion
    
    # The path to the config.yaml template.
    self.configTemplatePath = os.path.join(self.home,"config","icp%s-config-template.yaml" % self.icpVersion)
    if (not os.path.isfile(self.configTemplatePath)):
      raise ICPInstallationException("A configuration template file: %s for ICP v%s does not exist in the bootstrap script package." % (self.configTemplatePath,self.ICPVersion))
    #endIf
    
    self.etcHostsPlaybookPath = os.path.join(self.home,"playbooks","etc-hosts-add-entry.yaml")
    if (not os.path.isfile(self.etcHostsPlaybookPath)):
      raise ICPInstallationException("Playbook: %s, does not exist in the bootstrap script package." % (self.etcHostsPlaybookPath))
    #endIf
    
    
    if (not self.ICPArchivePath):
      raise ICPInstallationException("The ICPArchivePath must be provided in the stack parameters.")
    #endIf
    
    self.icpInstallImageFileName = os.path.basename(self.ICPArchivePath)
    if (TR.isLoggable(Level.FINER)):
      TR.finer(methodName,"ICP installation image file name: %s" % self.icpInstallImageFileName)
    #endIf
    
    # The root stack parameter InceptionTimeout is expected to be provided.    
    if (not self.InceptionTimeout):
      if (TR.isLoggable(Level.FINER)):
        TR.finer(methodName,"Defaulting inception timeout to 7200 seconds (2 hours).")
      #endIf
      self.inceptionTimeout = 7200
    else:
      # NOTE: CloudFormation parameters are strings regardless of the parameter Type being Number.
      self.inceptionTimeout = int(self.InceptionTimeout)
    #endIf
    
    TR.info(methodName,"ICP Inception container operation timeout is %d seconds." % self.inceptionTimeout)
    self.dockerClient = docker.from_env(timeout=self.inceptionTimeout)

    self.pkiDirectory = os.path.join(self.icpHome,"cluster","cfc-certs")
    self.pkiFileName = 'icp-router'
    self.CN = self.getClusterCN()

    # Root stack ID is in position 0 of the stackIds list
    self.stackIds = [ rootStackId ]
    nestedStacks =  self._getNestedStacks(rootStackId)
    if (not nestedStacks):
      raise AWSStackResourceException("The root stack is expected to have several nested stacks, but none were found.")
    #endIf
    self.stackIds.extend(nestedStacks)
    
    self._getHosts(self.stackIds)

    self._getSSMParameterKeys(rootStackName)
    
    self.rootStackOutputs = self.getStackOutputs(rootStackId)
        
  #endDef
  
  
  def addBootNodeSSHKeys(self):
    """
      If an ssh_publickeys file exists in the root home directory, then append that file
      to the ubuntu users authorized_keys file.  This allows additional administrators to 
      ssh into the boot node.
    """
    methodName = "addBootNodeSSHKeys"
    if (not os.path.exists("/root/ssh_publickeys")):
      TR.info(methodName,"No additional ssh public keys to include for access to the boot node.")  
    else:
      TR.info(methodName,"Adding SSH public keys to permit access to the boot node.")
      with open("/home/ubuntu/.ssh/authorized_keys", "a+") as authorized_keys, open("/root/ssh_publickeys","r") as ssh_publickeys:
        for publicKey in ssh_publickeys:
          publicKey = publicKey.rstrip()
          if (TR.isLoggable(Level.FINEST)):
            TR.finest(methodName,"To ubuntu user SSH authorized_keys adding:\n\t%s" % publicKey)
          #endIf
          authorized_keys.write("%s\n" % publicKey)
        #endFor
      #endWith
    #endIf
  #endDef
  
  
  def addHost(self,role,host):
    """
      Helper method to build out the hosts dictionary that holds the list 
      of hosts for each ICP role.
      
      role is a string that is expected to be one of ICPClusterRoles
      host is an instance of Host
    """
    methodName = "addHost"
    
    if (role in ICPClusterRoles):
      hostsInRole = self.hosts.get(role)
      hostsInRole.append(host)
      if (TR.isLoggable(Level.FINE)):
        TR.fine(methodName,"Added host with private DNS name: %s and with IP address: %s to role: %s" % (host.private_dns_name,host.ip4_address,role))
      #endIf
    else:
      TR.warn(methodName,"Unexpected host role: %s for host: %s.  Valid host roles: %s" % (role,host.ip4_address,ICPClusterRoles))
    #endIf
  #endIf
  
  def getRootStackOutput(self, outputName):
    """
      Return the root stack output with the given outputName
    """
    if (not self.rootStackOutputs):
      self.rootStackOutputs = self.getStackOutputs(self.rootStackId)
    #endIf
    
    return self.rootStackOutputs.get(outputName)
  #endDef
  
  
  def getProxyELBDNSName(self):
    """
      Return the proxy ELB DNS name.
      
      The proxy ELB DNS name is the ProxyNodeLoadBalancerName output of the root stack.  
    """ 
    name = self.getRootStackOutput('ProxyNodeLoadBalancerName')
    
    if (not name):
      raise AWSStackResourceException("The root stack: %s must have an output with key: ProxyNodeLoadBalancerName" % self.rootStackId)
    #endIf
    
    return name
  #endDef
  
  
  def addParameters(self,parameters,addedParameters):
    """
      Modify the parameters dictionary with any values in the addedParameters dictionary 
      that are not present in the parameters dictionary.
    """
    addedKeys = addedParameters.keys()
    for key in addedKeys:
      if (not parameters.get(key)):
        parameters[key] = addedParameters.get(key)
      #endIf
    #endFor
    return parameters
  #endDef
  
  
  def getStackOutputs(self, stackId):
    """
      Return a dictionary with the stack output name-value pairs from the
      CloudFormation stack with the given stackId.
      
      If the stack has no outputs an empty dictionary is returned.
    """
    result = {}
    stack = self.cfnResource.Stack(stackId)
    for output in stack.outputs:
      key = output['OutputKey']
      value = output['OutputValue']
      result[key] = value
    #endFor
    
    return result
  #endDef
  
  
  def getProxyELBHostedZoneId(self):
    """
      Return the hosted zone ID for this VPC.
      
      The HostedZone is one of the root stack outputs.
    """
    
    hostedZoneId = self.getRootStackOutput('ProxyELBHostedZoneID')
    
    if (not hostedZoneId): 
      raise AWSStackResourceException("The root stack: %s must have an output with key: ProxyELBHostedZone" % self.rootStackId)
    #endIf
    
    return hostedZoneId
  #endDef
  
  
  def addRoute53Aliases(self, aliases, target, targetHostedZoneId):
    """
      For each alias in the aliases list, add an alias entry for the target to Route53 DNS.
      
      If aliases is not a Python list, then it is assumed to be a string with comma separated items
      that represents the list.
      
      NOTE: Two hosted zone IDs are needed, one for the cluster and the other for the target.
    """
    methodName = "addRoute53Aliases"
    
    if (type(aliases) != type([])):
      aliases = [alias.strip() for alias in aliases.split(',')]
    #endIf
    
    clusterHostedZoneId = self.getRootStackOutput('ClusterHostedZoneId')
    
    if (not clusterHostedZoneId):
      raise AWSStackResourceException("The root stack: %s must have an output with key: ClusterHostedZoneId" % self.rootStackId)
    #endIf
    
    changes = []
    for alias in aliases:
      change = {
                 'Action': 'UPSERT',
                 'ResourceRecordSet': 
                  {
                    'Name': alias,
                    'Type': 'A',
                    'AliasTarget': 
                     {
                       'HostedZoneId': targetHostedZoneId,
                       'DNSName': target,
                       'EvaluateTargetHealth': False
                     }
                  }
               }
      if (TR.isLoggable(Level.FINE)):
        TR.fine(methodName,"Adding Route53 alias entry: %s >>>> %s" % (alias,target))
      #endIf
      changes.append(change)
    #endFor
    
    changeBatch = {'Comment': 'Create/update %s DNS aliases' % target, 'Changes': changes}
    response = self.route53.change_resource_record_sets(HostedZoneId=clusterHostedZoneId,ChangeBatch=changeBatch)
    
    if (not response):
      raise ICPInstallationException("Failed to update Route53 resource record(s)")
    #endIf
    
    if (TR.isLoggable(Level.FINE)):
      changeInfo = response.get('ChangeInfo')
      TR.fine(methodName, "Route53 DNS change request Id: %s has status: %s" % (changeInfo.get('Id'),changeInfo.get('Status')))
    #endIf
  #endDef
  
  
  def _getAutoScalingGroupEC2Instances(self, asgIds):
    """
      Return a list of EC2 instance IDs for the members of the given auto-scaling groups
      with the given auto-scaling-group IDs.
      
      If asgIds is not a list then it is assumed to be a strting and a list is formed
      with that string.
    """
    result = []
    
    if (not asgIds):
      raise InvalidArgumentException("An auto-scaling group ID or a list of auto-scaling group IDs (asgIds) is required.")
    #endIf
    
    if (type(asgIds) != type([])):
      asgIds = [asgIds]
    #endIf
    
    response = self.asg.describe_auto_scaling_groups( AutoScalingGroupNames=asgIds )
    if (not response):
      raise AWSStackResourceException("Empty result for AutoScalingGroup describe_auto_scaling_groups for asg: %s" % asgIds)
    #endIf
    
    autoScalingGroups = response.get('AutoScalingGroups')
    for asg in autoScalingGroups:
      instances = asg.get('Instances')
      for instance in instances:
        result.append(instance.get('InstanceId'))
      #endFor
    #endFor
    
    return result  
  #endDef
  
  
  def _getEC2Instances(self, stackId):
    """
      Return a list of EC2 instance IDs deployed in the given stack.
      The instances can be deployed atomically or as a member of an auto-scaling group.
      
      The returned list is intended to be used to get the roles and IP addresses
      of the members of the ICP cluster, to create the hosts file used by the 
      installer on the boot node.
    """
    result = []
    
    if (not stackId):
      raise MissingArgumentException("A stack ID (stackId) is required.")
    #endIf
    
    response = self.cfnClient.list_stack_resources(StackName=stackId)
    if (not response):
      raise AWSStackResourceException("Empty result for CloudFormation list_stack_resources for stack: %s" % stackId)
    #endIf
    
    stackResources = response.get('StackResourceSummaries')
    if (not stackResources):
      raise AWSStackResourceException("Empty StackResourceSummaries in response from CloudFormation list_stack_resources for stack: %s." % stackId)
    #endIf

    for resource in stackResources:
      resourceType = resource.get('ResourceType')
      if (resourceType == 'AWS::EC2::Instance'):
        ec2InstanceId = resource.get('PhysicalResourceId')
        result.append(ec2InstanceId)        
      #endIf
      if (resourceType == 'AWS::AutoScaling::AutoScalingGroup'):
        ec2InstanceIds = self._getAutoScalingGroupEC2Instances(resource.get('PhysicalResourceId'))
        result.extend(ec2InstanceIds)
      #endIf
    #endFor

    return result
  #endDef
  
  
  def _getNestedStacks(self, rootStackId):
    """
      Return a list of the nested stack resource IDs associated with the given root stack ID.
      
      By convention the nested stack resource IDs are provided by the CloudFormation root 
      stack template in the Outputs.  The output variable name is StackIds and it is a 
      string representation of a list of resource Ids where the separator is a comma.
      
      AWS CloudFormation does not support real list structure in the output values.
    
    """
    response = self.cfnClient.describe_stacks(StackName=rootStackId)
    if (not response):
      raise AWSStackResourceException("Empty result for CloudFormation describe_stacks for stack: %s" % rootStackId)
    #endIf
    
    stacks = response.get('Stacks')
    if (len(stacks) != 1):
      raise AWSStackResourceException("Unexpected number of stacks: %d, from describe_stacks for stack: %s" % (len(stacks),rootStackId))
    #endIf
    
    rootStack = stacks[0]
    outputs = rootStack.get('Outputs')
    if (not outputs):
      raise AWSStackResourceException("No outputs defined for the ICP root stack: %s" % rootStackId)
    #endIf
    
    nestedStackIds = None
    for output in outputs:
      key = output.get('OutputKey')
      if (key == 'StackIds'):
        nestedStackIds = output.get('OutputValue').split(',')
        break
      #endIf
    #endFor
    
    return nestedStackIds
  #endDef
  
  
  def _getHosts(self, stackIds):
    """
      Fill the hosts dictionary instance variable with Host objects for all the hosts in the
      cluster based on the ICPRole tag associated with each host (EC2 instance).
      
      The deployment is made up of a root stack and several nested stacks.
      
      The nested stackIds are in the Outputs section of the root stack.
      
      The root stack and other supporting stacks may not have any EC2 instances.
      
      If there are no EC2 instances in all of the stacks, then something is wrong and 
      an exception is raised.
      
      The hosts dictionary is used to create the hosts file needed for the ICP installation.
    """
    methodName = "_getHosts"
    
    if (not stackIds):
      raise InvalidArgumentException("A non-empty list of stack IDs (stackIds) is required.")
    #endIf
     
    if (TR.isLoggable(Level.FINEST)):
      TR.finest(methodName,"StackIds: %s" % stackIds)
    #endIf
    
    ec2InstanceIds = []
    for stackId in stackIds:
      ec2Ids = self._getEC2Instances(stackId)
      if (ec2Ids):
        ec2InstanceIds.extend(ec2Ids)
      #endIf
    #endFor
    
    if (not ec2InstanceIds):
      raise AWSStackResourceException("The ICP deployment is expected to have several EC2 instances, but none were found.")
    #endIf
    
    for iid in ec2InstanceIds:
      ec2Instance = self.ec2.Instance(iid)
      tags = ec2Instance.tags
      icpRole = ""
      for tag in tags:
        if tag['Key'] == 'ICPRole':
          icpRole = tag['Value'].lower()
        #endIf
      #endFor
      if (not icpRole):
        TR.warning(methodName,"Each EC2 instance in the cluster is expected to have an ICPRole tag.")
      elif (icpRole == "boot"):
        self.bootHost = Host(ec2Instance.private_ip_address,ec2Instance.private_dns_name,icpRole,iid)
      elif (icpRole in ICPClusterRoles):
        self.addHost(icpRole,Host(ec2Instance.private_ip_address,ec2Instance.private_dns_name,icpRole,iid))
      else:
        TR.warning(methodName,"Unexpected role: %s" % icpRole)
      #endIf
    #endFor
  #endDef
  
  
  def getMasterHosts(self):
    """
      Return the list of hosts instances in role of 'master'
      
      Convenience method for getting the hosts with the role of 'master'
    """
    return self.hosts.get('master')
  #endDef
  
  
  def getMasterIPAddresses(self):
    """
      Return a list of ip4 addresses for the hosts in role of 'master'
    """
    hosts = self.getMasterHosts()
    return [host.ip4_address for host in hosts]
  #endDef

  
  def getMasterDNSNames(self):
    """
      Return a list of private DNS names for the hosts in role of 'master'
    """
    hosts = self.getMasterHosts()
    return [host.private_dns_name for host in hosts]
  #endDef
  
  
  def getClusterHosts(self):
    """
      Return a list of Host instances for all the hosts in the ICP cluster.
      
      NOTE: The list gets cached in self.clusterHosts.  Always use getClusterHosts()
      but after the first invocation the cache is used.
    """
    if (not self.clusterHosts):
      result = []
      roles = self.hosts.keys()
      for role in roles:
        hostsInRole = self.hosts.get(role)
        if (hostsInRole):
          for host in hostsInRole:
            result.append(host)
          #endFor
        #endIf
      #endFor
      self.clusterHosts = result
    #endIf
    return self.clusterHosts
  #endDef


  def createEtcHostsFile(self):
    """
      Add content to the local /etc/hosts file that captures the private IP address
      and host name of boot node all the members of the cluster.
      
      NOTE: This method is not used. The VPC DNS service is enabled and it works
      for all EC2 instances deployed in the VPC.
    """      
    with open("/etc/hosts", "a+") as hosts:
      hosts.write("\n#### BEGIN IBM Cloud Private Cluster Hosts\n\n")
      hosts.write("%s\t\t%s\n" % (self.bootHost.ip4_address,self.bootHost.private_dns_name))
      for role in ICPClusterRoles:
        hostsInRole = self.hosts.get(role)
        if (hostsInRole):
          for host in hostsInRole:
            hosts.write("%s\t\t%s\n" % (host.ip4_address,host.private_dns_name))
          #endFor
        #endIf
      #endFor
      hosts.write("\n#### END IBM Cloud Private Cluster Hosts\n")
    #endWith
  #endDef

 
  def propagateEtcHostsFile(self):
    """
      Thin wrapper around runAnsiblePlaybook() to copy the local /etc/hosts/ file to all cluster nodes.
    """ 
    playbookPath = os.path.join(self.home,"playbooks","copy-etc-hosts.yaml")
    self.runAnsiblePlaybook(playbookPath,targetNodes='all')
  #endDef


  def _writeGroupToHostsFile(self,group,hosts,hostsFile):
    """
      Helper method for createAnsibleHostsFile()
      
      group is the name of the group and it defines a section 
            of the hosts file.
            
      hosts is the list of Host instances that are in the group
      
      hostsFile is an open file descriptor.
    """
    if (hosts):
      hostsFile.write("[%s]\n" % group)
      for host in hosts:
        hostsFile.write("%s\n" % host.ip4_address)
      #endFor
      hostsFile.write("\n")
    #endIf
  #endDef
  
  
  def _writeGroupOfGroupsToHostsFile(self,group,children,hostsFile):
    """
      Helper method for createAnsibleHostsFile()
      
      Write a INI style group of groups to an Ansible hosts file.
      
      group is the name of the group of groups and it defines a section
            of the hosts file.
            
      children is a list of groups that make up the group of groups,
               i.e., the child groups
             
      hostsFile is an open file descriptor
    """
    if (children):
      hostsFile.write("[%s:children]\n" % group)
      for child in children:
        hostsFile.write("%s\n" % child)
      #endFor
      hostsFile.write("\n")
    #endIf

  #endDef 
  
  def createICPHostsFile(self):
    """
      Create a proper ICP hosts file in the current working directory.
      This file serves as an Ansible inventory of hosts for the ICP inception
      container.
    """
    methodName = "createICPHostsFile"
    
    TR.info(methodName,"STARTED creating hosts file for the ICP installation.")
    with open("hosts", "w") as hostsFile:
      for role in ICPClusterRoles:
        hostsInRole = self.hosts.get(role)
        self._writeGroupToHostsFile(role,hostsInRole,hostsFile)
      #endFor
    #endWith
    TR.info(methodName,"COMPLETED creating hosts file for the ICP installation.")
  #endDef

  # TBD: At one point I thought I needed a group of groups to run an ansible
  # playbook to mount EFS volumes for the master and worker nodes.  The master
  # nodes have scripting that mounts the EFS volumes they need for registry, 
  # icp audit and k8s audit log.  The worker nodes need a mount and an entry
  # in /etc/fstab to use the EFS storage provisioner.
  def createAnsibleHostsFile(self):
    """
      Create a hosts file to be used with Ansible playbooks for the cluster.
      The boot node IP address is added to the Ansible hosts file.
      
      A separate Ansible hosts file is created for use outside of the hosts file
      used by the inception container to allow the boot node to be included in
      playbook targets.
      
      A group of groups is created for use as a target for mounting EFS storage.
      The group of groups is the master nodes and the worker nodes.  All need
      to mount the EFS storage and have an entry in /etc/fstab.
      
      NOTE: To run a playbook that picks up all nodes in the cluster use "all"
      for the nodes to target.
    """
    methodName = "createAnsibleHostsFile"
    
    TR.info(methodName, "STARTED configuring /etc/ansible/hosts file.")
    with open("/etc/ansible/hosts", "a+") as hostsFile:
      # Create a group for each role, including a group for the boot node 
      self._writeGroupToHostsFile('boot',[self.bootHost],hostsFile)
      self._writeGroupToHostsFile('icp',self.getClusterHosts(),hostsFile)
      for role in ICPClusterRoles:
        hostsInRole = self.hosts.get(role)
        self._writeGroupToHostsFile(role,hostsInRole,hostsFile)
      #endFor
      # TBD: I don't think I need this.
      #self._writeGroupOfGroupsToHostsFile('efs_client', ['master','worker'], hostsFile)
    #endWith
    TR.info(methodName,"COMPLETED configuring /etc/ansible/hosts file.")
  #endDef
  
  
  def createSSHKeyScanHostsFile(self):
    """
      Create a hosts file to be used with ssh-keyscan.
      The boot node needs to do an ssh-keyscan on all members of the cluster and itself.
      The ssh-keyscan hosts file is one IP address per line.
    """      
    with open("ssh-keyscan-hosts", "w") as hosts:
      hosts.write("%s\n" % self.bootHost.ip4_address)
      for role in ICPClusterRoles:
        hostsInRole = self.hosts.get(role)
        if (hostsInRole):
          for host in hostsInRole:
            hosts.write("%s\n" % host.ip4_address)
          #endFor
        #endIf
      #endFor
    #endWith
  #endDef


  def _deleteSSMParameters(self):
    """
      Cleanup method that deletes all the SSM parameters used to orchestrate the deployment.
      
      The SSMParameterKeys gets initialized in _getSSMParameterKeys() which gets called in 
      the _init() method after the cluster hosts have all been determined by introspecting
      the CloudFormation stack.
      
      No need to attempt to delete_parameters if SSMParameterKeys is empty, which it 
      might be if something bad happens before SSMParameterKeys gets initialized.
      
      NOTE: This method is intended to be executed in the finally block of the try-except
      in main() so if an exception occurs here it is caught here and the stack dump is
      emitted to the bootstrap log file.
      
      NOTE: The maximum number of keys that can be deleted in one call to delete_parameters()
      is 10.  Go figure. The number of SSMParameterKeys will be the number of nodes in the 
      cluster.  In the body of the method the SSMParameterKeys is broken up into a list of
      lists of length at most 10.
    """
    methodName = "_deleteSSMParameters"
    
    global SSMParameterKeys
    
    try:
      if (SSMParameterKeys):
        # keep within the limit of deleting at most 10 keys at a time
        parmKeys = [SSMParameterKeys[i:i+10] for i in range(0, len(SSMParameterKeys), 10)]
        for keys in parmKeys:
          self.ssm.delete_parameters(Names=keys)
          if (TR.isLoggable(Level.FINEST)):
            TR.finest(methodName,"Post install cleanup. Deleted SSM parameters: %s" % keys)
          #endIf
        #endFor
      #endIf
    except Exception as e:
      raise ICPInstallationException("Attempting to delete SSM parameter keys: %s\n\tException: %s" % (SSMParameterKeys,e))
    #endTry
    
  #endDef
  
  
  def generateSSHKey(self):
    """
      Create an SSH key pair for the boot node (this node).
      
      TODO - Add checks for a key already existing.
      In the context of AWS CloudFormation deployment, a key has not been created.
      
    """
    
    if (not os.path.exists(self.sshHome)):
      os.makedirs(self.sshHome)
    #endIf
    
    privateKeyPath = os.path.join(self.sshHome, 'id_rsa')
    
    self.sshKey = RSA.generate(4096) 
    with open(privateKeyPath, "w") as privateKeyFile:
      privateKeyFile.write(self.sshKey.exportKey('PEM'))
      chmod(privateKeyPath, 0600)
    #endWith
    
    publicKeyPath = os.path.join(self.sshHome, 'id_rsa.pub')
    
    publicKey = self.sshKey.publickey()
    with open(publicKeyPath, "w") as publicKeyFile:
      publicKeyFile.write(publicKey.exportKey('OpenSSH'))
    #endWith
  #endDef
  
  
  def addAuthorizedKey(self):
    """
      Add the boot node public key to the root ~/.ssh/authorized_keys file.
    """
    if (not os.path.exists(self.sshHome)):
      os.makedirs(self.sshHome)
    #endIf
    
    authKeysPath = os.path.join(self.sshHome, 'authorized_keys')
    
    publicKey = self.sshKey.publickey()
    self.authorizedKeyEntry ="%s root@%s" % (publicKey.exportKey('OpenSSH'),self.bootHost.ip4_address)
    with open(authKeysPath, "a+") as authorized_keys:
      authorized_keys.write("%s\n" % self.authorizedKeyEntry)
    #endWith
  #endDef
  
  
  def publishSSHPublicKey(self, stackName, authorizedKeyEntry):
    """
      Publish the boot node SSH public key string to an SSM parameter with name <StackName>/boot-public-key
    """
    methodName = "publishSSHPublicKey"
    
    parameterKey = "/%s/boot-public-key" % stackName
    
    TR.info(methodName,"Putting SSH public key to SSM parameter: %s" % parameterKey)
    self.ssm.put_parameter(Name=parameterKey,
                           Description="Root public key and private IP address for ICP boot node to be added to autorized_keys of all ICP cluster nodes.",
                           Value=authorizedKeyEntry,
                           Type='String',
                           Overwrite=True)
    TR.info(methodName,"Public key published.")
    
  #endDef

  def syncWithClusterNodes(self,desiredState='READY'):
    """
      Wait for all cluster nodes to indicate they are ready to proceed with the installation.
    """
    methodName = "syncWithClusterNodes"
    
    
    hostsNotReady = self.getClusterHosts()
    
    tryCount = 1
    while (hostsNotReady and tryCount <= ClusterHostSyncMaxTryCount):
      for host in hostsNotReady:
        hostParameter = "/%s/%s" % (self.rootStackName,host.private_dns_name)
        if (TR.isLoggable(Level.FINE)):
          TR.fine(methodName,"Try: %d, checking readiness of host: %s with role: %s, using SSM Parameter: %s" % (tryCount,host.private_dns_name,host.role,hostParameter))
        #endIf
        try:
          response = self.ssm.get_parameter(Name=hostParameter)
          if (not response):
            TR.warning(methodName, "Failed to get a response for get_parameter: %s" % hostParameter)
          else:
            parameter = response.get('Parameter')
            if (not parameter):
              raise Exception("get_parameter response returned an empty Parameter.")
            #endIf
            state = parameter.get('Value')
            if (state == desiredState):
              TR.info(methodName,"Role: %s, host: %s, is in desired state: %s." % (host.role,host.private_dns_name,state))
              hostsNotReady.remove(host)
              self.putSSMParameter(hostParameter,'ACK',description="Acknowledged state: %s received." % state)
            else:
              if (TR.isLoggable(Level.FINEST)):
                TR.finest(methodName,"Role: %s, host: %s, Ignoring state: %s" % (host.role,host.private_dns_name,state))
              #endIf
            #endIf
          #endIf
        except ClientError as e:
          etext = "%s" % e
          if (etext.find('ParameterNotFound') >= 0):
            if (TR.isLoggable(Level.FINEST)):
              TR.finest(methodName,"Ignoring ParameterNotFound ClientError on ssm.get_parameter() invocation")
            #endIf
          else:
            raise ICPInstallationException("Unexpected ClientError on ssm.get_parameter() invocation: %s" % etext)
          #endIf
        #endTry
      #endFor
      time.sleep(ClusterHostSyncSleepTime)
      tryCount += 1
    #endWhile
    
    if (hostsNotReady):
      raise Exception("Cluster hosts are not ready: %s" % hostsNotReady)
    else:
      TR.info(methodName, "All cluster hosts are ready to proceed with the ICP installation.")
    #endIf
  #endDef
  

  def waitForStackStatus(self, stackId, desiredStatus='CREATE_COMPLETE'):
    """
      Return True if the given stack reaches the given desired status.
      
      This method is ensure that the bootnode script doesn't start doing its work until the 
      CloudFormation engine has fully deployed the root stack template.
      
      An ICPInstallationException is raised if the desiredStatus is not achieved within  the
      status wait timeout = wait_time * number_of_times_waited.
    """
    methodName = 'waitForStackStatus'
    
    reachedDesiredStatus = False
    waitCount = 1
    while (not reachedDesiredStatus and waitCount <= StackStatusMaxWaitCount):
      TR.info(methodName,"Try: %d: Waiting for stack status: %s for stack: %s" % (waitCount,desiredStatus,stackId))
      response = self.cfnClient.describe_stacks(StackName=stackId)
      if (not response):
        raise AWSStackResourceException("Empty result for CloudFormation describe_stacks for stack: %s" % stackId)
      #endIf
      
      stacks = response.get('Stacks')
      if (len(stacks) != 1):
        raise AWSStackResourceException("Unexpected number of stacks: %d, from describe_stacks for stack: %s" % (len(stacks),stackId))
      #endIf
      
      stack = stacks[0]
      currentStatus = stack.get('StackStatus')
      if (currentStatus == desiredStatus):
        reachedDesiredStatus = True
        break
      #endIf
      
      TR.info(methodName,"Stack: %s current status: %s, waiting for status: %s" % (stackId,currentStatus,desiredStatus))
      time.sleep(StackStatusSleepTime)
    #endWhile
    
    if (not reachedDesiredStatus):
      raise ICPInstallationException("Stack: %s never reached status: %s after waiting %d minutes." % (stackId,desiredStatus,(StackStatusSleepTime*StackStatusMaxWaitCount)/60))
    #endIf
    
    TR.info(methodName,"Stack: %s in desired state: %s" % (stackId,desiredStatus))
    return reachedDesiredStatus
  #endDef
  
  
  def configureSSH(self):
    """
      Configure the boot node (where this script is assumed to be running) to be able to
      do passwordless SSH as root to all the nodes in the cluster.
    """
    self.generateSSHKey()
    self.addAuthorizedKey()
    self.publishSSHPublicKey(self.rootStackName,self.authorizedKeyEntry)
  #endDef
  
  
  def sshKeyScan(self):
    """
      Do an SSH keyscan to pick up the ecdsa/rsa fingerprint for each host in the cluster. 
      
      The boot node needs all the ecdsa/rsa fingerprints from all cluster nodes and itself.
    """
    methodName = "sshKeyScan"
    
    knownHostsPath = os.path.join(self.sshHome, "known_hosts")
#    keyscanStdErr = os.path.join(os.path.expanduser("~"),"logs","keyscan.log")
    
    try:
      with open(knownHostsPath,"a+") as knownHostsFile:
        TR.info(methodName,"STARTED ssh-keyscan for hosts in ssh-keyscan-hosts file: %s." % knownHostsPath)
        retcode = call(["ssh-keyscan", "-4", "-t", "rsa", "-f", "ssh-keyscan-hosts" ], stdout=knownHostsFile )
        if (retcode != 0):
          raise Exception("Error calling ssh-keyscan. Return code: %s" % retcode)
        else:
          TR.info(methodName,"COMPLETED SSH keyscan.")
        #endIf
      #endWith
    except Exception as e:
      raise ICPInstallationException("Error calling ssh-keyscan: %s" % e)
    #endTry
    
  #endDef
  
  
  def putSSMParameter(self,parameterKey,parameterValue,description=""):
    """
      Put the given parameterValue to the given parameterKey
      
      Wrapper for dealing with CloudFormation SSM parameters.
    """
    methodName = "putSSMParameter"
    
    TR.info(methodName,"Putting value: %s to SSM parameter: %s" % (parameterValue,parameterKey))
    self.ssm.put_parameter(Name=parameterKey,
                           Description=description,
                           Value=parameterValue,
                           Type='String',
                           Overwrite=True)
    TR.info(methodName,"Value: %s put to: %s." % (parameterValue,parameterKey))
    
  #endDef
  
  
  def getS3Object(self, bucket=None, s3Path=None, destPath=None):
    """
      Return destPath which is the local file path provided as the destination of the download.
      
      A pre-signed URL is created and used to download the object from the given S3 bucket
      with the given S3 key (s3Path) to the given local file system destination (destPath).
      
      The destination path is assumed to be a full path to the target destination for 
      the object. 
      
      If the directory of the destPath does not exist it is created.
      It is assumed the objects to be gotten are large binary objects.
      
      For details on how to download a large file with the requests package see:
      https://stackoverflow.com/questions/16694907/how-to-download-large-file-in-python-with-requests-py
    """
    methodName = "getS3Object"
    
    if (not bucket):
      raise MissingArgumentException("An S3 bucket name (bucket) must be provided.")
    #endIf
    
    if (not s3Path):
      raise MissingArgumentException("An S3 object key (s3Path) must be provided.")
    #endIf
    
    if (not destPath):
      raise MissingArgumentException("A file destination path (destPath) must be provided.")
    #endIf
    
    TR.info(methodName, "STARTED download of object: %s from bucket: %s, to: %s" % (s3Path,bucket,destPath))
    
    s3url = self.s3.generate_presigned_url(ClientMethod='get_object',Params={'Bucket': bucket, 'Key': s3Path})
    if (TR.isLoggable(Level.FINE)):
      TR.fine(methodName,"Getting S3 object with pre-signed URL: %s" % s3url)
    #endIf
    
    destDir = os.path.dirname(destPath)
    if (not os.path.exists(destDir)):
      os.makedirs(destDir)
      TR.info(methodName,"Created object destination directory: %s" % destDir)
    #endIf
    
    r = requests.get(s3url, stream=True)
    with open(destPath, 'wb') as destFile:
      shutil.copyfileobj(r.raw, destFile)
    #endWith

    TR.info(methodName, "COMPLETED download from bucket: %s, object: %s, to: %s" % (bucket,s3Path,destPath))
    
    return destPath
  #endDef
  
  
  def getInstallImages(self):
    """
      Create a presigned URL and use it to download the ICP and Docker images from the S3
      bucket where the images are stored.
      
      CloudFormation input parameters used in this method:
        ICPArchiveBucketName
        ICPArchivePath 
        DockerInstallBinaryPath 
        
        Docker binary gets downloaded to: /root/docker/icp-install-docker.bin
        ICP install image gets downloaded to: /tmp/icp-install-archive.tgz
        
      NOTE: If the image files already exist, then nothing is done.  (The image files may be 
      copied to the desired location in the local file system using a ConfigSet as part of the
      instantiation of the boot node in the CloudFormation template.  
      
      Using a pre-signed URL is needed when the deployer does not have access to the installation
      image bucket.
    """
    methodName = "getInstallImages"
    
    icpImagePath = "/tmp/icp-install-archive.tgz"
    dockerBinaryPath = "/root/docker/icp-install-docker.bin"
    
    if (not os.path.isfile(icpImagePath)):
      TR.info(methodName,"Getting object: %s from bucket: %s using a pre-signed URL." % (self.ICPArchivePath,self.ICPArchiveBucketName))
      self.getS3Object(bucket=self.ICPArchiveBucketName,s3Path=self.ICPArchivePath,destPath=icpImagePath)
    else:
      TR.info(methodName,"ICP installation image already exists: %s" % icpImagePath)
    #endIf
    
    if (not os.path.isfile(dockerBinaryPath)):
      TR.info(methodName,"Getting object: %s from bucket: %s using a pre-signed URL." % (self.DockerInstallBinaryPath,self.ICPArchiveBucketName))
      self.getS3Object(bucket=self.ICPArchiveBucketName,s3Path=self.DockerInstallBinaryPath,destPath=dockerBinaryPath)
    else:
      TR.info(methodName,"Docker installation binary already exists: %s" % dockerBinaryPath)
    #endIf
  #endDef
  
  
  def installDocker(self, inventoryPath='/etc/ansible/hosts'):
    """
      Use an instance of the InstallDocker helper class to run an Ansible playbook
      to install docker on the boot node and all cluster member nodes.
      
      NOTE: This does not work due to issues with getting the Ansible library 
      set up and importing modules in the InstallDocker module.  Didn't have
      time to figure out the Ansible Python library structure to get the 
      imports corrected.
    """
    methodName = "installDocker"
    
#    installer = InstallDocker(inventoryPath)
    
    TR.info(methodName, "Running the install-docker.yaml playbook.")
#    installer.runPlaybook("./playbooks/install-docker.yaml", {'target_nodes': 'worker' })
    
    TR.info(methodName,"Running the config-docker.yaml playbook.")
#   installer.runPlaybook("./playbooks/config-docker.yaml", {'target_nodes': 'worker'})
  #endDef
  
  
  def _getDockerImage(self,rootName):
    """
      Return a docker image instance for the given rootName if it is available in 
      the local registry.
      
      Helper for installKubectl() and any other method that needs to get an image
      instance from the local docker registry.
    """
    result = None
    
    imageList = self.dockerClient.images.list()

    for image in imageList:
      imageNameTag = image.tags[0]
      if (imageNameTag.find(rootName) >= 0):
        result = image
        break
      #endIf
    #endFor
    return result
  #endDef
  
  
  def installKubectl(self):
    """
      Copy kubectl out of the icp-inception image to /usr/local/bin
      Convenient for troubleshooting.  
      
      If the kubernetes image is not available then this method is a no-op.
    """
    methodName = "installKubectl"
    
    TR.info(methodName,"STARTED install of kubectl to local host /usr/local/bin.")
    kubeImage = self._getDockerImage("icp-inception")
    if (not kubeImage):
      TR.info(methodName,"An icp-inception image is not available. Kubectl WILL NOT BE INSTALLED.")
    else:
      kubeImageName = kubeImage.tags[0]
      
      TR.info(methodName,"An icp-inception image: %s, is available in the local docker registry.  Proceeding with the installation of kubectl." % kubeImageName)
      if (TR.isLoggable(Level.FINEST)):
        TR.finest(methodName,"%s image tags: %s" % (kubeImageName,kubeImage.tags))
      #endIf
      
      self.dockerClient.containers.run(kubeImageName,
                                       network_mode='host',
                                       volumes={"/usr/local/bin": {'bind': '/data', 'mode': 'rw'}},
                                       environment=["LICENSE=accept"],
                                       command="cp /usr/local/bin/kubectl /data"
                                       )
      
      configKubectl = ConfigureKubectl(user='root',clusterName=self.getClusterName(),masterNode=self.getMasterIPAddresses()[0])
      configKubectl.configureKube()
    #endIf
    TR.info(methodName,"COMPLETED install of kubectl to local host /usr/local/bin.")
  #endDef


  def restartKubeletAndDocker(self):
    """
      Run Ansible playbook to restart kubelet and docker on all cluster nodes.
      
      The playbook does the following on all cluster nodes (boot node excluded)
        - stop kubelet
        - stop docker
        - start docker
        - start kubelet
        
      NOTE: This method was used in an attempt to clear up an early installation issue.
      It is no longer part of the installation process and eventually can probably be 
      deleted. 
      TBD: It may be handy to have this method for other purposes. It is often the case that
      a sick node can be healed with a restart of kubelet and docker. If that is the case
      then a "target" parameter would need to be passed in and forwarded on to the Ansible
      script.  
    """
    methodName = "restartKubeletAndDocker"
    
    TR.info(methodName,"STARTED restart of kubelet and docker on all cluster nodes.")
    
    playbookPath = os.path.join(self.home,"playbooks","restart-kubelet-and-docker.yaml")
    self.runAnsiblePlaybook(playbookPath=playbookPath,targetNodes="icp")
    
    TR.info(methodName,"COMPLETED restart of kubelet and docker on all cluster nodes.")
  #endDef
  

  def runAnsiblePlaybook(self, playbookPath=None, targetNodes="all", inventory="/etc/ansible/hosts"):
    """
      Invoke a shell script to run an Ansible playbook with the given arguments.
      
      NOTE: Work-around because I can't get the Ansible Python libraries figured out on Unbuntu.
    """
    methodName = "runAnsiblePlaybook"
    
    if (not playbookPath):
      raise MissingArgumentException("The playbook path (playbookPath) must be provided.")
    #endIf
    
    try:
      TR.info(methodName,"Executing ansible-playbook with: playbook=%s, nodes=%s, inventory=%s." % (playbookPath,targetNodes,inventory))
      retcode = call(["ansible-playbook", playbookPath, "--extra-vars", "target_nodes=%s" % targetNodes, "--inventory", inventory ] )
      if (retcode != 0):
        raise Exception("Error calling ansible-playbook. Return code: %s" % retcode)
      else:
        TR.info(methodName,"ansible-playbook: %s completed." % playbookPath)
      #endIf
    except Exception as e:
      TR.error(methodName,"Error calling ansible-playbook: %s" % e, e)
      raise
    #endTry    
  #endDef

  
  # NOTE: Not using this for now.  The fixpack fails to install when a private registry is used.
  #       We have to have the fixpack for ICP to run on AWS.
  # WARNING: The code associated with configuring a private registry has not been tested at all.
  def configurePrivateRegistry(self):
    """
      Use the PrivateRegistry class to configure a Docker private registry to be used
      for the IBM Cloud Private installation.
    """
    raise NotImplementedException("The configurePrivateRegistry method is not implemented yet.")
  
    privateRegistry = PrivateRegistry(serverPKIDirectory=self.serverPKIDirectory,
                                      clientPIKDirectory=self.clientPKIDirectory,
                                      bits=4096,
                                      CN=self.fqdn)
    privateRegistry.configurePrivateRegistry()
  #endIf
  
  
  def configureEFS(self):
    """
      Configure an EFS volume and configure all worker nodes to be able to use 
      the EFS storage provisioner.
    """
    methodName = "configureEFS"
    
    TR.info(methodName,"STARTED configuration of EFS on all worker nodes.")
    # Configure shared storage for applications to use the EFS provisioner
    # This commented out code is obsolete.  The boot node is on the public
    # subnet and cannot access the EFS mount targets on the private subnets.
    #efsServer = self.EFSDNSName                    # An input to the boot node
    #mountPoint = self.ApplicationStorageMountPoint # Also a boot node input
    #efsVolumes = EFSVolume(efsServer,mountPoint)
    #self.mountEFSVolumes(efsVolumes)
    
    # Configure EFS storage on all of the worker nodes.
    playbookPath = os.path.join(self.home,"playbooks","configure-efs-mount.yaml")
    varTemplatePath = os.path.join(self.home,"playbooks","efs-var-template.yaml")
    manifestTemplatePath = os.path.join(self.home,"config","efs","manifest-template.yaml")
    rbacTemplatePath = os.path.join(self.home,"config","efs","rbac-template.yaml")
    serviceAccountPath = os.path.join(self.home,"config","efs","service-account.yaml")
    configEFS = ConfigureEFS(region=self.AWSRegion,
                             stackId=self.bootStackId,
                             playbookPath=playbookPath,
                             varTemplatePath=varTemplatePath,
                             manifestTemplatePath=manifestTemplatePath,
                             rbacTemplatePath=rbacTemplatePath,
                             serviceAccountPath=serviceAccountPath)
    
    configEFS.configureEFS()
    TR.info(methodName,"COMPLETED configuration of EFS on all worker nodes.")
  #endDef
  
  
  def createConfigFile(self):
    """
      Create a configuration file from a template and based on stack parameter values.
    """
    methodName="createConfigFile"
    
    configureICP = ConfigureICP(stackIds=self.stackIds, 
                                configTemplatePath=self.configTemplatePath,
                                etcHostsPlaybookPath=self.etcHostsPlaybookPath)
    
    TR.info(methodName,"STARTED creating config.yaml file.")
    configureICP.createConfigFile(os.path.join(self.home,"config.yaml"),self.ICPVersion)
    TR.info(methodName,"COMPLETED creating config.yaml file.")
    
  #endDef
  
  
  def loadICPImages(self,imageArchivePath):
    """
      Load the IBM Cloud Private images from the installation tar archive.
      
      The boot node must load the images as part of the pre-installation steps.
      
      The AWS CloudFormation template downlaods the ICP installation tar ball from
      an S3 bucket to /tmp/icp-install-archive.tgz of each cluster node.  It turns 
      out that download is very fast: typically 3 to 4 minutes.
    """
    methodName = "loadICPImages"
        
    TR.info(methodName,"STARTED Docker load of ICP installation images.")
    
    retcode = call("tar -zxvf %s -O | docker load | tee /root/logs/load-icp-images.log" % imageArchivePath, shell=True)
    if (retcode != 0):
      raise ICPInstallationException("Error calling: 'tar -zxvf %s -O | docker load' - Return code: %s" % (imageArchivePath,retcode))
    #endIf
    
    TR.info(methodName,"COMPLETED Docker load of ICP installation images.")  
    
  #endDef
  

  def configureInception(self):
    """
      Do the pre-installation steps of getting the inception meta-data from the inception image
      and moving the files into place for hosts, ssh_key, and config.yaml
      
    """
    methodName = "configureInception"
    
    TR.info(methodName,"IBM Cloud Private Inception configuration started.")
    
    if (not os.path.exists(self.icpHome)):
      os.makedirs(self.icpHome)
    #endIf
    
    try:
      TR.info(methodName,"Extracting ICP meta data from the inception container to %s" % self.icpHome)
      
      if (TR.isLoggable(Level.FINER)):
        TR.finer(methodName,"Invoking: docker run -v %s:/data -e LICENSE=accept %s cp -r cluster /data" % (self.icpHome,self.InceptionImageName))
      #endIf
      
      self.dockerClient.containers.run(self.InceptionImageName, 
                                       volumes={self.icpHome: {'bind': '/data', 'mode': 'rw'}}, 
                                       environment={'LICENSE': 'accept'},
                                       command="cp -r cluster /data")
      
    except Exception as e:
      raise ICPInstallationException("ERROR invoking: 'docker run -v %s:/data -e LICENSE=accept %s cp -r cluster /data' - Exception: %s" % (self.icpHome,self.InceptionImageName,e))
    #endTry
    
    os.mkdir("%s/cluster/images" % self.icpHome)
    shutil.move("/tmp/icp-install-archive.tgz","%s/cluster/images/%s" % (self.icpHome,self.icpInstallImageFileName))
    shutil.copyfile("/root/hosts", "%s/cluster/hosts" % self.icpHome)
    shutil.copyfile("/root/config.yaml", "%s/cluster/config.yaml" % self.icpHome)
    shutil.copyfile("/root/.ssh/id_rsa", "%s/cluster/ssh_key" % self.icpHome)
    
    pkiconfig = {
      'bits': 4096,
      'CN': self.CN
      }
    configurePKI = ConfigurePKI(pkiDirectory=self.pkiDirectory,pkiFileName=self.pkiFileName,**pkiconfig)
    pkiParms = configurePKI.getPKIParameters()
    configurePKI.createKeyCertPair(**pkiParms)
    
    TR.info(methodName,"IBM Cloud Private Inception configuration completed.")    
  #endDef


  def loadInceptionFixpackImages(self):
    """
      Load the images from the inception fixpack archive into the local docker registry.

      NOTE: The fixpack inception archive is expected to be in /tmp/icp-inception-fixpack.tar
      The CloudFormation stack template should have included as step to copy the inception 
      fixpack tar from an S3 bucket to /tmp/icp-inception-fixpack.tar.
      
      WARNING: The ICP inception fixpack is not a real tar archive.  You can't use tar -xvf 
      on it.  You get an error complaining about an "invalid tar header."
      
      WARNING: The ICP inception fixpack also does not load from the docker SDK images.load() 
      method.  TBD - Is there some way to load the tar ball we are creating using something
      from the Docker SDK for Python?
      
      The only thing that works is: docker load -i /tmp/icp-inception-fixpack.tar
    
    """
    methodName = "loadInceptionFixpackImages"
    
    if (not os.path.isfile(self.inceptionFixpackArchivePath)):
      raise ICPInstallationException("Inception fixpack archive (.tar) file does not exist at: %s" % self.inceptionFixpackArchivePath)
    #endIf
    
    TR.info(methodName,"STARTED Docker load of inception fixpack images.")
    retcode = call("docker load -i /tmp/icp-inception-fixpack.tar | tee /root/logs/load-icp-inception-fixpack.log", shell=True)
    if (retcode != 0):
      raise ICPInstallationException("Error calling: 'docker load -i /tmp/icp-inception-fixpack.tar' - Return code: %s" % retcode)
    #endIf
    TR.info(methodName,"COMPLETED Docker load of inception fixpack images.")
    
  #endDef
  

  def runInceptionInstall(self,inceptionImage,commandString="install -v",logFilePath="/root/logs/icp-install.log"):
    """
      Run the inception install command.
      
      If logFilePath is None or empty then no log file is streamed.
      
    """
    methodName = "runInception"

    TR.info(methodName,"STARTED IBM Cloud Private Inception operation: %s" % commandString)  
    
    try:
      if (TR.isLoggable(Level.FINER)):
        TR.finer(methodName,"Invoking: docker run --net=host -t -e LICENSE=accept -v %s:/installer/cluster %s %s" % (self.icpHome,inceptionImage,commandString))
      #endIf
      
      inceptionContainer = self.dockerClient.containers.run(inceptionImage,
                                       network_mode='host',
                                       tty=True,
                                       environment={'LICENSE': 'accept'},
                                       volumes={"%s/cluster" % self.icpHome: {'bind': '/installer/cluster', 'mode': 'rw'}}, 
                                       command=commandString,
                                       detach=True)
      
      if (logFilePath):
        with open(logFilePath, "a") as icpInstallLogFile:
          for line in inceptionContainer.logs(stream=True):
            icpInstallLogFile.write(line)
          #endFor
        #endWith
      #endIf
      
      TR.info(methodName,"WAITING for inception operation: %s" % commandString)
      
      response = inceptionContainer.wait(timeout=self.inceptionTimeout, condition='not-running')
      
      statusCode = response.get('StatusCode')
      if (statusCode):
        raise ICPInstallationException("Inception container exited with a non-zero status code: %s" % statusCode)
      #endIf
      
    except Exception as e:
      TR.error(methodName,"ERROR invoking: 'docker run --net=host -t -e LICENSE=accept -v %s:/installer/cluster %s %s'\n\tException: %s" % (self.icpHome,inceptionImage,commandString,e))
      raise
    #endTry

    TR.info(methodName,"COMPLETED IBM Cloud Private Inception operation: %s" % commandString)        
  #endDef
  
  
  def setSourceDestCheck(self, instanceId, value):
    """
      Set the SourceDestCheck attribute for the given EC2 intsance to the given value.
      
    """
    methodName = "setSourceDestCheck"
    
    instance = self.ec2.Instance(instanceId)
    TR.info(methodName,"EC2 instance: %s, current value of source_dest_check: %s" % (instanceId,instance.source_dest_check))
    instance.modify_attribute(SourceDestCheck={ 'Value': value })
    TR.info(methodName,"EC2 instance: %s, new value of source_dest_check: %s" % (instanceId,value))
    
  #endDef
  
  
  def _disableSourceDestCheck(self):
    """
      In the context of a Kubernetes cluster the SourceDestCheck needs to be disabled.
      
      For auto-scaling groups the SourceDestCheck attribute is not exposed either in 
      the LaunchConfiguration resource or in the AutoScalingGroup resource, so we set
      it here for all cluster members.
    """
    methodName = "_disableSoureDestCheck"
    
    TR.info(methodName,"STARTED disabling of source_dest_check on all cluster members.")
    hosts = self.getClusterHosts()
    for host in hosts:
      self.setSourceDestCheck(host.instanceId, False)
    #endFor
    TR.info(methodName,"COMPLETED disabling of source_dest_check on all cluster members.")

  #endDef
  
  
  def installICPFixpack(self):
    """
      Install the ICP fixpack.
      
      WARNING: This method and the supporting code artifacts is implemented to 
      install ICP 2.1.0.3 fixpack1.  The way a fixpack gets installed may change
      for future releases of ICP.
      
      The fixpack will only install successfully on a cluster that already has
      the base ICP 2.1.0.3 installed.
      
      The ReadMe.html packaged with the fixpack gives the impression that you need
      to first using the fixpack inception image to install the ICP 2.1.0.3 base.
      Then use the base inception image to install the fixpack.
      
    """
    methodName = "installICPFixpack"
    
    TR.info(methodName,"STARTED IBM Cloud Private Fixpack installation.")
    
    srcFilePath = "/tmp/icp-fixpack.sh"
    dstFilePath = "%s/cluster/%s" % (self.icpHome,self.FixpackFileName)
    TR.info(methodName,"Moving fixpack file from %s to %s" % (srcFilePath,dstFilePath))
    shutil.move(srcFilePath,dstFilePath)
    TR.info(methodName,"Fixpack file moved to %s" % dstFilePath)
    
    self.loadInceptionFixpackImages()

    # NOTE: The inception install generates a log file in <icp_home>/cluster/logs
    self.runInceptionInstall(self.FixpackInceptionImageName,self.FixpackInstallCommandString,logFilePath=None)
    
    TR.info(methodName,"COMPLETED IBM Cloud Private Fixpack installation.")
    
  #endDef
  
  
  def installICPWithFixpack(self):
    """
      Install ICP with the inception fixpack image and install the ICP fixpack.
      
      WARNING: This method, and the supporting code artifacts, is implemented to 
      install ICP 2.1.0.3 fixpack1.  The way a fixpack gets installed may change
      for future releases of ICP.
      
      WARNING: The "ReadMe.html" that comes with the fixpack is erroneous.
      
      The process must be:
      1. Install ICP 2.1.0.3 using the image tar ball and the ibmcom/icp-inception:2.1.0.3-ee inception image.
      2. Then load the ibmcom/icp-inception:2.1.0.3-ee-fp1 image from the fixpack .tar file.
      3. Then install the fixpack using ibmcom/icp-inception:2.1.0.3-ee-fp1 and the 
         ./cluster/ibm-cloud-private-2.1.0.3-fp1.sh command string.
      The ReadMe with the fixpack has things inverterd. Could have been a cut-and-paste error.
      
    """
    methodName = "installICPWithFixpack"
    
    if (not self.FixpackInceptionImageName):
      raise ICPInstallationException("The FixpackInceptionImageName parameter must be provided in the ICP CloudFormation deployment stack.")
    #endIf
    
    TR.info(methodName,"STARTED IBM Cloud Private installation.")

    # NOTE: The ICP inception install generates a log in <icp_home>/cluster/logs
    self.runInceptionInstall(self.InceptionImageName,self.InceptionInstallCommandString,logFilePath=None)

    self.installICPFixpack()
    TR.info(methodName,"COMPLETED IBM Cloud Private installation.")

  #endDef


  def installICP(self):
    """
      Install ICP.
      
      It is assumed all the pre-installation configuration steps have been completed.
    """
    methodName = "installICP"

    TR.info(methodName,"IBM Cloud Private installation started.")

    self.runInceptionInstall(self.InceptionImageName,self.InceptionInstallCommandString,logFilePath=None)

    TR.info(methodName,"IBM Cloud Private installation completed.")
    
  #endDef


  def exportLogs(self, bucketName, stackName, logsDirectoryPath):
    """
      Export the deployment logs to the given S3 bucket for the given stack.
      
      Each log will be exported using a path with the stackName at the root and the 
      log file name as the next element of the path.
      
      NOTE: Prefer not to use trace in this method as the bootstrap log file has 
      already had the "END" message emitted to it.
    """
    methodName = "exportLogs"
    
    if (not os.path.exists(logsDirectoryPath)):
      if (TR.isLoggable(Level.FINE)):
        TR.fine(methodName, "Logs directory: %s does not exist." % logsDirectoryPath)
      #endIf
    else:
      logFileNames = os.listdir(logsDirectoryPath)
      if (not logFileNames):
        if (TR.isLoggable(Level.FINE)):
          TR.fine(methodName,"No log files in %s" % logsDirectoryPath)
        #endIf
      else:
        for fileName in logFileNames:
          s3Key = "%s/%s/%s/%s" % (stackName,self.role,self.fqdn,fileName)
          bodyPath = os.path.join(logsDirectoryPath,fileName)
          if (TR.isLoggable(Level.FINE)):
            TR.fine(methodName,"Exporting log: %s to S3: %s:%s" % (bodyPath,bucketName,s3Key))
          #endIf
          self.s3.put_object(Bucket=bucketName, Key=s3Key, Body=bodyPath)
        #endFor
      #endIf
    #endIf
  #endDef
  
  
  def mountEFSVolumes(self, volumes):
    """
      Mount the EFS storage volumes for the audit log and the Docker registry.
      
      volumes is either a singleton instance of EFSVolume or a list of instances
      of EFSVolume.  EFSVolume has everything needed to mount the volume on a
      given mount point.
 
      NOTE: It is assumed that nfs-utils (RHEL) or nfs-common (Ubuntu) has been
      installed on the nodes were EFS mounts are implemented.
     
      Depending on what EFS example you look at the options to the mount command vary.
      The options used in this method are from this AWS documentation:
      https://docs.aws.amazon.com/efs/latest/ug/wt1-test.html
      Step 3.3 has the mount command template and the options are:
      nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport
      
      The following defaults options are also included:
        rw,suid,dev,exec,auto,nouser
      
      WARNING: The boot node is on the public subnet and may not have access to 
      the EFS mount targets on the private subnets.  The security group may 
      be configured such that only private subnets can get to the EFS server 
      mount targets.
    """
    methodName = "mountEFSVolumes"
    
    if (not volumes):
      raise MissingArgumentException("One or more EFS volumes must be provided.")
    #endIf
    
    if (type(volumes) != type([])):
      volumes = [volumes]
    #endIf

    # See method doc above for AWS source for mount options used in the loop body below.
    options = "rw,suid,dev,exec,auto,nouser,nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport"
    
    for volume in volumes:
      if (not os.path.exists(volume.mountPoint)):
        os.makedirs(volume.mountPoint)
        TR.info(methodName,"Created directory for EFS mount point: %s" % volume.mountPoint)
      elif (not os.path.isdir(volume.mountPoint)):
        raise Exception("EFS mount point path: %s exists but is not a directory." % volume.mountPoint)
      else:
        TR.info(methodName,"EFS mount point: %s already exists." % volume.mountPoint)
      #endIf
      retcode = call("mount -t nfs4 -o %s %s:/ %s" % (options,volume.efsServer,volume.mountPoint), shell=True)
      if (retcode != 0):
        raise Exception("Error return code: %s mounting to EFS server: %s with mount point: %s" % (retcode,volume.efsServer,volume.mountPoint))
      #endIf
      TR.info(methodName,"%s mounted on EFS server: %s:/ with options: %s" % (volume.mountPoint,volume.efsServer,options))
    #endFor
  #endDef

  
  def main(self,argv):
    """
      Main does command line argument processing, sets up trace and then kicks off the methods to
      do the work.
    """
    methodName = "main"

    self.rc = 0
    try:
      ####### Start command line processing
      cmdLineArgs = Utilities.getInputArgs(self.ArgsSignature,argv[1:])

      # if trace is set from command line the trace variable holds the trace spec.
      trace, logFile = self._configureTraceAndLogging(cmdLineArgs)

      if (cmdLineArgs.get("help")):
        self._usage()
        raise ExitException("After emitting help, jump to the end of main.")
      #endIf

      beginTime = Utilities.currentTimeMillis()
      TR.info(methodName,"BOOT0101I BEGIN Bootstrap AWS ICP Quickstart version @{VERSION}.")

      if (trace):
        TR.info(methodName,"BOOT0102I Tracing with specification: '%s' to log file: '%s'" % (trace,logFile))
      #endIf
      
      rootStackId = cmdLineArgs.get('root-stackid')
      if (not rootStackId):
        raise MissingArgumentException("The root stack ID (--root-stackid) must be provided.")
      #endIf

      self.rootStackId = rootStackId
      TR.info(methodName,"Root stack ID: %s" % rootStackId)
      
      rootStackName = cmdLineArgs.get('stack-name')
      if (not rootStackName):
        raise MissingArgumentException("The root stack name (--stack-name) must be provided.")
      #endIf
      
      self.rootStackName = rootStackName
      TR.info(methodName,"Root stack name: %s" % rootStackName)

      bootStackId = cmdLineArgs.get('stackid')
      if (not bootStackId):
        raise MissingArgumentException("The boot stack ID (--stackid) must be provided.")
      #endIf

      self.bootStackId = bootStackId
      TR.info(methodName,"Boot stack ID: %s" % bootStackId)
      
      region = cmdLineArgs.get('region')
      if (not region):
        raise MissingArgumentException("The AWS region (--region) must be provided.")
      #endIf
      
      self.AWSRegion = region
      TR.info(methodName,"AWS region: %s" % region)
      
      role = cmdLineArgs.get('role')
      if (not role):
        raise MissingArgumentException("The role of this node (--role) must be provided.")
      #endIf
      
      self.role = role
      TR.info(methodName,"Node role: %s" % role)
      
      # Need to wait for the root stack to be fully deployed to get its outputs for
      # the introspection of all the child stacks.
      self.waitForStackStatus(rootStackId,desiredStatus='CREATE_COMPLETE')
      
      # Finish off the initialization of the bootstrap class instance
      self._init(rootStackId,rootStackName,bootStackId)
      
      # Using Route53 DNS server rather than /etc/hosts
      # WARNING - Discovered the hard way that the installation overwrites the /etc/hosts file
      # with the cluster IP address and all the other entries are lost.  Happens very late in the install.
      #self.createEtcHostsFile()
      #self.propagateEtcHostsFile()
      
      self.createICPHostsFile()
      self.createAnsibleHostsFile()
      self.configureSSH()
      self.addBootNodeSSHKeys()
 
      # Turn off source/dest check on all cluster EC2 instances
      self._disableSourceDestCheck()    
     
      # Wait for cluster nodes to be ready for the installation to proceed.
      # Waiting to make sure all cluster nodes have added the boot node
      # SSH public key to their SSH authorized_keys file.
      self.syncWithClusterNodes(desiredState='READY')
      
      self.createSSHKeyScanHostsFile()
      self.sshKeyScan()
      
      self.getInstallImages()
      
      # Add Route53 DNS aliases for the proxy ELB
      # TODO: Leave this commented out until we figure out how to delete the entry when the stack is deleted.
      #self.addRoute53Aliases(self.ApplicationDomains, self.getProxyELBDNSName(), self.getProxyELBHostedZoneId())
      
      # set vm.max_map_count on all cluster members
      setMaxMapCountPlaybookPath = os.path.join(self.home,"playbooks", "set-vm-max-mapcount.yaml")
      self.runAnsiblePlaybook(playbookPath=setMaxMapCountPlaybookPath,targetNodes="all")
      
      installDockerPlaybookPath = os.path.join(self.home,"playbooks", "install-docker.yaml")
      self.runAnsiblePlaybook(playbookPath=installDockerPlaybookPath,targetNodes="all")
      
      # Notify all cluster nodes that docker installation has completed.
      # Cluster nodes will proceed with loading ICP installation images locally from the ICP install archive. 
      self.putSSMParameter("/%s/docker-installation" % self.rootStackName,"COMPLETED",description="Docker installation status.")

      self.createConfigFile()
      
      self.loadICPImages(self.imageArchivePath)

      self.configureInception()

      # Wait for notification from all nodes that the local ICP image load has completed.
      self.syncWithClusterNodes(desiredState='READY')
      
      if (Utilities.toBoolean(self.InstallICPFixpack)):
        self.installICPWithFixpack()             
      else:
        self.installICP()        
      #endIf

      # Install kubectl includes configuration of a permanent login context so this
      # needs to happen after the installation of ICP to get configuration artifacts.
      self.installKubectl()
      
      # Configuring EFS and the EFS provisioner needs to happen after kubectl is configured. 
      self.configureEFS()
    
    except ExitException:
      pass # ExitException is used as a "goto" end of program after emitting help info

    except Exception, e:
      TR.error(methodName,"Exception: %s" % e, e)
      self.rc = 1

    finally:
      
      try:
        self._deleteSSMParameters()
      
        # Copy the deployment logs in self.logsHome and icpHome/logs to the S3 bucket for logs.
        self.exportLogs(self.ICPDeploymentLogsBucketName,self.rootStackName,self.logsHome)
        self.exportLogs(self.ICPDeploymentLogsBucketName,self.rootStackName,"%s/cluster/logs" % self.icpHome)
      except Exception, e:
        TR.error(methodName,"Exception: %s" % e, e)
        self.rc = 1
      #endTry

      
      endTime = Utilities.currentTimeMillis()
      elapsedTime = (endTime - beginTime)/1000
      etm, ets = divmod(elapsedTime,60)
      eth, etm = divmod(etm,60) 

      if (self.rc == 0):
        TR.info(methodName,"BOOT0103I END Boostrap AWS ICP Quickstart.  Elapsed time (hh:mm:ss): %d:%02d:%02d" % (eth,etm,ets))
      else:
        TR.info(methodName,"BOOT0104I FAILED END Boostrap AWS ICP Quickstart.  Elapsed time (hh:mm:ss): %d:%02d:%02d" % (eth,etm,ets))
      #endIf
      
    #endTry


    if (TR.traceFile):
      TR.closeTraceLog()
    #endIf

    sys.exit(self.rc)
  #endDef

#endClass

if __name__ == '__main__':
  mainInstance = Bootstrap()
  mainInstance.main(sys.argv)
#endIf
